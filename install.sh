#!/usr/bin/env bash
set -euo pipefail
umask 077

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
data_dir="${HOME}/.config/adbot"
non_interactive=0
skip_login=0
skip_deps=0
replace_env=0
service=ask

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
usage() {
    printf '%s\n' 'Usage: ./install.sh [--data-dir DIR] [--systemd | --no-systemd]' \
        '                    [--non-interactive] [--skip-login] [--skip-system-deps] [--replace-env]' \
        'Non-interactive mode reads TELEGRAM_API_ID, TELEGRAM_API_HASH, BOT_TOKEN and OWNER_ID.' \
        'It requires an existing Telegram session unless --skip-login is specified.'
}

while (($#)); do
    case "$1" in
        --data-dir)
            if (($# < 2)) || [[ -z $2 ]]; then die "--data-dir requires a directory"; fi
            data_dir=$2
            shift
            ;;
        --non-interactive) non_interactive=1 ;;
        --skip-login) skip_login=1 ;;
        --skip-system-deps) skip_deps=1 ;;
        --replace-env) replace_env=1 ;;
        --systemd) service=yes ;;
        --no-systemd) service=no ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
    shift
done
if (( ! non_interactive )) && [[ ! -t 0 ]]; then
    die "interactive setup requires a terminal; use --non-interactive for scripted installs"
fi
data_dir=$(realpath -m -- "$data_dir")

ask() {
    local name=$1 label=$2 hidden=${3:-0} value default=${!1-}
    if [[ -n $default ]]; then label+=" [Enter keeps the environment value]"; fi
    if (( hidden )); then
        read -rs -p "$label: " value || die "setup cancelled"
        printf '\n' >&2
    else
        read -r -p "$label: " value || die "setup cancelled"
    fi
    printf -v "$name" '%s' "${value:-$default}"
}

confirm() {
    local answer
    read -r -p "$1 [y/N]: " answer || die "setup cancelled"
    [[ $answer == [yY] || $answer == [yY][eE][sS] ]]
}

owner_ids_valid() {
    [[ $1 =~ ^[[:blank:]]*[1-9][0-9]{0,18}[[:blank:]]*(,[[:blank:]]*[1-9][0-9]{0,18}[[:blank:]]*)*$ ]]
}

campaign=0
if (( ! non_interactive )); then
    printf '%s\n' 'Telegram adbot setup. All configuration choices come first.'
    ask TELEGRAM_API_ID 'API ID from my.telegram.org → API development tools'
    ask TELEGRAM_API_HASH 'API hash from my.telegram.org → API development tools' 1
    if (( ! skip_login )); then
        ask TELEGRAM_PHONE 'Phone number with country code, e.g. +15551234567'
    fi
    ask BOT_TOKEN 'Token from @BotFather /newbot (blank for CLI-only)' 1
    if [[ -n ${BOT_TOKEN:-} ]]; then
        ask OWNER_ID 'Owner user IDs, comma-separated (blank uses the logged-in account)'
    fi
    if [[ $service == ask ]] && confirm 'Install and start adbot.service at boot?'; then service=yes; fi
    if confirm 'Configure a first campaign (disabled until you enable it)?'; then
        campaign=1
        ask ADBOT_CAMPAIGN_NAME 'Campaign name, e.g. weekend_sale'
        ask ADBOT_CAMPAIGN_MESSAGE 'Ad text (one line)'
        ask ADBOT_CAMPAIGN_INTERVAL 'Repeat interval, e.g. 30m or 2h'
        ask ADBOT_CAMPAIGN_GROUPS 'Joined group IDs, @usernames or Telegram links, separated by spaces'
    fi
elif [[ -n ${ADBOT_CAMPAIGN_NAME:-}${ADBOT_CAMPAIGN_MESSAGE:-}${ADBOT_CAMPAIGN_INTERVAL:-}${ADBOT_CAMPAIGN_GROUPS:-} ]]; then
    campaign=1
fi

api_id=${TELEGRAM_API_ID:-}
api_hash=${TELEGRAM_API_HASH:-}
phone=${TELEGRAM_PHONE:-}
bot_token=${BOT_TOKEN:-}
owner_id=${OWNER_ID:-}
if [[ ! $api_id =~ ^[1-9][0-9]{0,9}$ ]] || (( api_id > 2147483647 )); then
    die "TELEGRAM_API_ID must be a positive 32-bit integer from my.telegram.org"
fi
[[ $api_hash =~ ^[[:xdigit:]]{32}$ ]] || die "TELEGRAM_API_HASH must be the 32-character hexadecimal hash from my.telegram.org"
if [[ -n $phone ]] || (( ! non_interactive && ! skip_login )); then
    [[ $phone =~ ^\+[1-9][0-9]{5,14}$ ]] || die "TELEGRAM_PHONE must include the country code, e.g. +15551234567"
fi
if [[ -n $bot_token ]]; then
    [[ $bot_token =~ ^[1-9][0-9]*:[a-zA-Z0-9_-]+$ ]] || die "BOT_TOKEN must be a token from @BotFather"
    if [[ -n $owner_id ]]; then
        owner_ids_valid "$owner_id" || die "OWNER_ID must contain positive numeric Telegram user IDs separated by commas"
        owner_id=${owner_id//[$' \t']/}
    elif (( skip_login )); then
        die "set OWNER_ID if you provide BOT_TOKEN with --skip-login"
    fi
else
    owner_id=
fi

if (( campaign )); then
    [[ ${ADBOT_CAMPAIGN_NAME:-} =~ ^[a-zA-Z0-9_-]{1,64}$ ]] || die "invalid initial campaign name"
    message=${ADBOT_CAMPAIGN_MESSAGE:-}
    if [[ ! $message =~ [^[:space:]] ]] || (( ${#message} > 4096 )); then
        die "initial campaign text must contain 1–4096 characters"
    fi
    [[ ${ADBOT_CAMPAIGN_INTERVAL:-} =~ ^[1-9][0-9]{0,8}[smhdSMHD]$ ]] || die "invalid initial interval (try 30m or 2h)"
    read -r -a groups <<< "${ADBOT_CAMPAIGN_GROUPS:-}"
    [[ -n ${groups[0]:-} ]] || die "give at least one joined group for the initial campaign"
fi

if [[ -e $data_dir/.env || -L $data_dir/.env ]] && (( ! replace_env )); then
    if (( non_interactive )) || ! confirm "Replace the existing .env in $data_dir?"; then
        die "existing .env was not changed (use --replace-env to replace it explicitly)"
    fi
fi
if [[ $service == yes ]]; then
    (( ! skip_login )) || die "--systemd requires a Telegram session; do not combine it with --skip-login"
    (( EUID != 0 )) || die "run as a normal sudo-enabled user, not root, to install the service"
    command -v systemctl >/dev/null || die "systemctl is not available on this VPS"
    [[ ! -e /etc/systemd/system/adbot.service && ! -L /etc/systemd/system/adbot.service ]] ||
        die "adbot.service already exists; see the README for updating an installation"
    case "$repo$data_dir" in
        *$'\r'*|*$'\n'*) die "service paths must not contain line breaks" ;;
    esac
fi

python_ready() {
    command -v python3 >/dev/null && python3 -c 'import ensurepip, venv, sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null
}

needs_deps=0
if ! python_ready; then
    (( ! skip_deps )) || die "Python 3.10+ and its venv/ensurepip support must be installed"
    if [[ ! -r /etc/os-release ]] || ! command -v apt-get >/dev/null; then
        die "install Python 3.10+ and venv support first"
    fi
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ ${ID:-} == ubuntu || ${ID:-} == debian ]] || die "automatic system package installation supports Ubuntu and Debian only"
    needs_deps=1
fi

as_root=()
if (( EUID != 0 )) && { (( needs_deps )) || [[ $service == yes ]]; }; then
    command -v sudo >/dev/null || die "sudo is required to install packages or the system service"
    if (( non_interactive )); then
        as_root=(sudo -n)
    else
        sudo -v || die "could not authorize sudo to install packages or the system service"
        as_root=(sudo)
    fi
fi

privileged() {
    "${as_root[@]}" "$@" ||
        die "privileged command failed: $*. Review the error above and check sudo access for this command."
}

if (( needs_deps )); then
    privileged apt-get update
    privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv
    python_ready || die "this distribution did not provide Python 3.10+ with venv support"
fi

cd -- "$repo"
mkdir -p -- "$data_dir"
if [[ ! -x .venv/bin/python ]]; then python3 -m venv .venv; fi
if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
    .venv/bin/python -m ensurepip --upgrade ||
        die "could not restore pip in .venv; recreate the virtual environment and rerun setup"
fi
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
run=("$repo/.venv/bin/python" -m adbot --data-dir "$data_dir")

temporary=$(mktemp "$data_dir/.env.XXXXXX.tmp")
trap 'if [[ -n $temporary ]]; then rm -f -- "$temporary"; fi' EXIT
write_settings() {
    printf 'TELEGRAM_API_ID=%s\nTELEGRAM_API_HASH=%s\nBOT_TOKEN=%s\nOWNER_ID=%s\n' \
        "$api_id" "$api_hash" "$bot_token" "$owner_id" > "$temporary"
}

run_setup() {
    TELEGRAM_API_ID="$api_id" TELEGRAM_API_HASH="$api_hash" BOT_TOKEN="$bot_token" OWNER_ID="$owner_id" "${run[@]}" "$@"
}

write_settings
if (( ! skip_login )); then
    if (( ! non_interactive )); then
        printf '%s\n' 'Sign in now using the Telegram code and, if enabled, your 2FA password.'
        TELEGRAM_PHONE="$phone" run_setup login
    fi
    if (( non_interactive )) || [[ -n $bot_token && -z $owner_id ]]; then
        account_id=$(run_setup whoami) || die "Telegram login is required; run the installer interactively or use --skip-login without --systemd"
        if [[ -n $bot_token && -z $owner_id ]]; then
            owner_ids_valid "$account_id" || die "could not determine your Telegram user ID"
            owner_id=$account_id
            write_settings
        fi
    fi
fi

if (( campaign )); then
    group_flags=()
    for group in "${groups[@]}"; do group_flags+=("--group=$group"); done
    run_setup add "$ADBOT_CAMPAIGN_NAME" --message="$message" --interval="$ADBOT_CAMPAIGN_INTERVAL" "${group_flags[@]}" --disabled
fi
mv -fT -- "$temporary" "$data_dir/.env"
temporary=

unit_arg() {
    local path=${1//\\/\\\\}
    path=${path//\"/\\\"}
    path=${path//%/%%}
    printf '"%s"' "$path"
}

if [[ $service == yes ]]; then
    temporary=$(mktemp "$data_dir/adbot.service.XXXXXX.tmp")
    printf '%s\n' '[Unit]' 'Description=Telegram group ads scheduler' 'Wants=network-online.target' 'After=network-online.target' \
        '' '[Service]' "User=$(id -un)" "WorkingDirectory=${repo//%/%%}" \
        "ExecStart=$(unit_arg ":$repo/.venv/bin/python") -m adbot --data-dir $(unit_arg "$data_dir") run" \
        'Restart=on-failure' 'RestartSec=10' 'UMask=0077' '' '[Install]' 'WantedBy=multi-user.target' > "$temporary"
    privileged install -m 0644 -- "$temporary" /etc/systemd/system/adbot.service
    privileged systemctl daemon-reload
    privileged systemctl enable --now adbot.service
    printf '%s\n' 'Installed and started adbot.service. Check: systemctl status adbot'
else
    if (( skip_login )); then
        printf 'Login was skipped. Before first use: cd %q && ' "$repo"
        printf '%q ' "${run[@]}" login; printf '\n'
    fi
    printf 'Start the bot with: cd %q && ' "$repo"
    printf '%q ' "${run[@]}" run; printf '\n'
fi
