import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("bash") and shutil.which("realpath") and os.name == "posix", "needs Bash and Linux tools")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.repo = root / "repo with spaces"
        self.repo.mkdir()
        shutil.copy(ROOT / "install.sh", self.repo)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        self.unit = root / "adbot.service"
        self.trace = root / "commands.log"
        self.data = root / "private data"
        self.env = {
            "HOME": str(root),
            "PATH": f"{bin_dir}:{os.defpath}",
            "TRACE": str(self.trace),
            "UNIT": str(self.unit),
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "a" * 32,
        }
        self.executable(bin_dir / "python3", """#!/bin/bash
set -eu
if [[ $1 = -c ]]; then
    [[ ${PYTHON_DEPS_MISSING:-0} = 0 || -e $TRACE.deps-installed ]]
    exit 0
fi
[[ "$*" = '-m venv .venv' ]]
echo venv >> "$TRACE"
mkdir -p .venv/bin
cp "${0%/*}/venv-python" .venv/bin/python
""")
        self.executable(bin_dir / "venv-python", """#!/bin/bash
set -eu
if [[ $2 = pip ]]; then
    if [[ ${PIP_MISSING:-0} = 1 && ! -e $TRACE.pip-restored ]]; then exit 1; fi
    if [[ $3 = --version ]]; then exit 0; fi
    echo pip >> "$TRACE"
    exit 0
fi
if [[ $2 = ensurepip ]]; then
    echo ensurepip >> "$TRACE"
    touch "$TRACE.pip-restored"
    exit 0
fi
[[ $1 = -m && $2 = adbot && $3 = --data-dir && -n $TELEGRAM_API_ID && -n $TELEGRAM_API_HASH ]]
if [[ -n ${EXPECT_OLD_ENV:-} ]]; then [[ $(< "$4/.env") = "$EXPECT_OLD_ENV" ]]; fi
shift 4
printf '%s\n' "$@" >> "$TRACE"
if [[ $1 = "${FAIL_COMMAND:-}" ]]; then exit 1; fi
if [[ $1 = whoami ]]; then echo 100; fi
""")
        self.executable(bin_dir / "systemctl", "#!/bin/bash\nexit 1\n")
        self.executable(bin_dir / "sudo", """#!/bin/bash
set -eu
if [[ ${INTERACTIVE_SUDO:-0} = 1 ]]; then
    [[ $1 != -n ]]
    if [[ $1 = -v ]]; then
        echo sudo-validate >> "$TRACE"
        exit 0
    fi
else
    [[ $1 = -n ]]
    shift
fi
if [[ "$*" = "${FAIL_PRIVILEGED:-}" ]]; then exit 1; fi
unset DEBIAN_FRONTEND
case "$1" in
    apt-get|env) "$@" ;;
    install) [[ $6 = /etc/systemd/system/adbot.service ]]; cp -- "$5" "$UNIT" ;;
    systemctl) echo "$*" >> "$TRACE" ;;
    *) exit 1 ;;
esac
""")

    def executable(self, path, source):
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)

    def install(self, *flags):
        return subprocess.run(
            [str(self.repo / "install.sh"), "--non-interactive", "--skip-system-deps",
             "--data-dir", str(self.data), *flags],
            env=self.env, cwd=self.repo.parent, capture_output=True, text=True, timeout=15,
        )

    def test_offline_setup_writes_private_credentials_and_disabled_campaign(self):
        self.env.update({
            "BOT_TOKEN": "123:offline-test-token",
            "OWNER_ID": "100 , 200",
            "TELEGRAM_PHONE": "+15551234567",
            "ADBOT_CAMPAIGN_NAME": "weekend",
            "ADBOT_CAMPAIGN_MESSAGE": "Weekend sale",
            "ADBOT_CAMPAIGN_INTERVAL": "2h",
            "ADBOT_CAMPAIGN_GROUPS": "@test_group -1001234567890",
        })
        result = self.install("--skip-login", "--no-systemd")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.data / ".env"
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(config.read_text(), f"TELEGRAM_API_ID=12345\nTELEGRAM_API_HASH={'a' * 32}\n"
                                            "BOT_TOKEN=123:offline-test-token\nOWNER_ID=100,200\n")
        self.assertNotIn(self.env["BOT_TOKEN"], result.stdout)
        self.assertEqual(self.trace.read_text().splitlines(), [
            "venv", "pip", "add", "weekend", "--message=Weekend sale", "--interval=2h",
            "--group=@test_group", "--group=-1001234567890", "--disabled",
        ])

        self.env["TELEGRAM_API_HASH"] = "b" * 32
        rejected = self.install("--skip-login")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("existing .env was not changed", rejected.stderr)
        self.assertIn("a" * 32, config.read_text())

    def test_invalid_credentials_fail_before_creating_files(self):
        self.env["TELEGRAM_API_HASH"] = "invalid"
        result = self.install("--skip-login")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TELEGRAM_API_HASH must be", result.stderr)
        self.assertFalse(self.data.exists())
        self.assertFalse(self.trace.exists())

    def test_replacement_settings_commit_only_after_login_and_campaign_succeed(self):
        self.data.mkdir()
        settings = self.data / ".env"
        old_settings = "TELEGRAM_API_ID=999"
        settings.write_text(old_settings + "\n", encoding="utf-8")
        self.env.update({
            "EXPECT_OLD_ENV": old_settings,
            "ADBOT_CAMPAIGN_NAME": "weekend",
            "ADBOT_CAMPAIGN_MESSAGE": "Weekend sale",
            "ADBOT_CAMPAIGN_INTERVAL": "2h",
            "ADBOT_CAMPAIGN_GROUPS": "@test_group",
        })
        for command in ("whoami", "add"):
            with self.subTest(command=command):
                self.env["FAIL_COMMAND"] = command
                result = self.install("--no-systemd", "--replace-env")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(settings.read_text(), old_settings + "\n")
                self.assertEqual(list(self.data.glob(".env.*.tmp")), [])
        del self.env["FAIL_COMMAND"]
        result = self.install("--no-systemd", "--replace-env")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TELEGRAM_API_ID=12345\n", settings.read_text())

    def test_existing_virtualenv_without_pip_is_repaired(self):
        result = self.install("--skip-login")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.env["PIP_MISSING"] = "1"
        result = self.install("--skip-login", "--replace-env")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace.read_text().splitlines(), ["venv", "pip", "ensurepip", "pip"])

    def test_existing_session_sets_owner_without_prompting(self):
        self.env["BOT_TOKEN"] = "123:offline-test-token"
        result = self.install("--no-systemd")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OWNER_ID=100\n", (self.data / ".env").read_text())
        self.assertEqual(self.trace.read_text().splitlines(), ["venv", "pip", "whoami"])

    @unittest.skipIf(getattr(os, "geteuid", lambda: 0)() == 0 or Path("/etc/systemd/system/adbot.service").exists(),
                     "requires a non-root test user without an installed adbot service")
    def test_service_uses_virtualenv_and_existing_session(self):
        result = self.install("--systemd")
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = self.unit.read_text()
        self.assertIn(f"WorkingDirectory={self.repo}\n", unit)
        self.assertIn(f'ExecStart=":{self.repo}/.venv/bin/python" -m adbot --data-dir "{self.data}" run\n', unit)
        self.assertIn("Restart=on-failure\n", unit)
        self.assertIn("UMask=0077\n", unit)
        self.assertEqual(self.trace.read_text().splitlines(), [
            "venv", "pip", "whoami", "systemctl daemon-reload",
            "systemctl enable --now adbot.service",
        ])

    @unittest.skipIf(getattr(os, "geteuid", lambda: 0)() == 0 or Path("/etc/systemd/system/adbot.service").exists(),
                     "requires a non-root test user without an installed adbot service")
    def test_interactive_service_authorizes_sudo_before_login(self):
        self.env.update({"INTERACTIVE_SUDO": "1", "TELEGRAM_PHONE": "+15551234567"})
        master, terminal = os.openpty()
        try:
            os.write(master, b"\n\n\n\ny\nn\n")
            result = subprocess.run(
                [str(self.repo / "install.sh"), "--skip-system-deps", "--data-dir", str(self.data)],
                stdin=terminal, env=self.env, cwd=self.repo.parent,
                capture_output=True, text=True, timeout=15,
            )
        finally:
            os.close(terminal)
            os.close(master)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace.read_text().splitlines(), [
            "sudo-validate", "venv", "pip", "login", "systemctl daemon-reload",
            "systemctl enable --now adbot.service",
        ])

    @unittest.skipIf(getattr(os, "geteuid", lambda: 0)() == 0, "requires a non-root test user")
    def test_apt_install_sets_frontend_after_sudo(self):
        release = Path("/etc/os-release")
        if not release.is_file() or not any(
            line.removeprefix("ID=").strip("\"'") in {"ubuntu", "debian"}
            for line in release.read_text().splitlines() if line.startswith("ID=")
        ):
            self.skipTest("requires Ubuntu or Debian")
        self.env["PYTHON_DEPS_MISSING"] = "1"
        self.executable(self.repo.parent / "bin" / "apt-get", """#!/bin/bash
set -eu
case "$*" in
    update) echo apt-update >> "$TRACE" ;;
    'install -y python3 python3-venv')
        [[ ${DEBIAN_FRONTEND:-} = noninteractive ]]
        echo apt-install >> "$TRACE"
        touch "$TRACE.deps-installed"
        ;;
    *) exit 1 ;;
esac
""")
        result = subprocess.run(
            [str(self.repo / "install.sh"), "--non-interactive", "--skip-login",
             "--no-systemd", "--data-dir", str(self.data)],
            env=self.env, cwd=self.repo.parent, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace.read_text().splitlines(), ["apt-update", "apt-install", "venv", "pip"])

    @unittest.skipIf(getattr(os, "geteuid", lambda: 0)() == 0 or Path("/etc/systemd/system/adbot.service").exists(),
                     "requires a non-root test user without an installed adbot service")
    def test_privileged_failure_identifies_the_command(self):
        self.env["FAIL_PRIVILEGED"] = "systemctl daemon-reload"
        result = self.install("--systemd")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("privileged command failed: systemctl daemon-reload", result.stderr)
        self.assertNotIn("systemctl enable", self.trace.read_text())
