# Telegram group ads userbot

Schedule recurring text ads from your own Telegram account to groups you select. The userbot only posts to groups the account has joined; it does not join groups or send private messages.

**Only post where group administrators allow automated ads.** Follow the group rules and [Telegram's Terms of Service](https://telegram.org/tos). Repeated or unwanted messages can get your account restricted. Telegram may impose additional limits at any time.

## Install on a VPS

Use Ubuntu 22.04+, Debian 12+, or another version with Python 3.10 or newer. Connect with a non-root SSH user that has `sudo` access:

```sh
ssh ubuntu@YOUR_VPS_IP
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/faresbazed/telegram-group-ads-userbot.git
cd telegram-group-ads-userbot
./install.sh
```

The installer asks for everything up front: your API ID and hash ([my.telegram.org](https://my.telegram.org) → **API development tools**), your phone number with country code, and optionally a token from [@BotFather](https://t.me/BotFather) (`/newbot`). If you supply a token, leave the owner ID blank to use the Telegram account you sign in with, or enter a comma-separated list of numeric Telegram user IDs to explicitly grant others full campaign control. You can also enter a first campaign; it starts disabled.

Answer **yes** to install `adbot.service` and start it at boot. The installer installs missing Python and venv packages, creates `.venv`, and then asks for the Telegram login code and your 2FA password if required. It saves the new settings to a private `~/.config/adbot/.env` only after login and any initial campaign setup succeed. The phone number, login code, and 2FA password are not saved in `.env`. If you configured a bot token, open your bot in Telegram and send `/start` in a private chat. Only reasonably trusted people should be listed as owners.

When system packages are missing or you choose to install the service, the interactive installer runs `sudo -v` before Telegram login, so you can enter your sudo password up front. If that authorization expires during setup, sudo may prompt again.

**Check status and logs** (when the system service was installed):

```sh
systemctl status adbot --no-pager
sudo journalctl -u adbot -f
```

**Update** from your existing checkout to keep the same settings, Telegram session, and campaign data:

```sh
cd ~/telegram-group-ads-userbot
git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
sudo systemctl restart adbot
systemctl status adbot --no-pager
```

**Uninstall** the service:

```sh
sudo systemctl disable --now adbot
sudo rm /etc/systemd/system/adbot.service
sudo systemctl daemon-reload
```

You can then remove the checkout and its virtual environment. Keep `~/.config/adbot` if you want to retain your credentials, Telegram session, and campaigns. If you no longer need the session, revoke it from **Settings → Devices** in Telegram before deleting the data directory.

### Unattended setup

Inject `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from your deployment environment rather than entering secrets into shell history. Optionally inject `BOT_TOKEN` and `OWNER_ID`. With an existing authorized session in the data directory, run:

```sh
./install.sh --non-interactive --systemd
```

Only the privileged commands actually used need passwordless or already-authorized `sudo`; command-specific sudo rules are supported. First-time Telegram sign-in still requires the interactive code and, if enabled, a 2FA password. To only prepare the installation, run `./install.sh --non-interactive --skip-login --no-systemd`; provide `OWNER_ID` yourself if setting a bot token in this mode. Use `--data-dir DIR` to choose another data directory. The installer will not replace an existing `.env` without confirmation or `--replace-env`; use the update commands above for routine updates. To supply an optional first campaign non-interactively, set `ADBOT_CAMPAIGN_NAME`, `ADBOT_CAMPAIGN_MESSAGE`, `ADBOT_CAMPAIGN_INTERVAL`, and space-separated `ADBOT_CAMPAIGN_GROUPS`.

## Manual setup

Requires Python 3.10+. From the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
mkdir -p -m 700 "$HOME/.config/adbot"
cp .env.example "$HOME/.config/adbot/.env"
chmod 600 "$HOME/.config/adbot/.env"
```

Get your own API ID and hash from [my.telegram.org](https://my.telegram.org), then set them in `$HOME/.config/adbot/.env`. Environment variables take precedence over this file. Log in interactively with your phone number, Telegram login code and 2FA password if prompted:

```sh
python -m adbot login
python -m adbot list-groups
```

The data directory holds your credentials, Telegram session, campaigns and schedule state. A session file grants access to your account; keep it private and never commit or share it. For a second account or an isolated test, put `--data-dir PATH` before the subcommand and use a separate directory.

## Manage campaigns in Telegram

1. In Telegram, open [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token into `BOT_TOKEN` in your data directory's `.env`. Keep the token private.
2. After `python -m adbot login`, run `python -m adbot whoami` to get your numeric Telegram user ID. Set `OWNER_ID` to that ID. If you intentionally want to give additional Telegram users full control of your campaigns, add their numeric user IDs separated by commas.
3. Run `python -m adbot run`, open your bot's **private** chat and send `/start`. The bot ignores group chats and users outside the allowlist. Leave `BOT_TOKEN` unset if you only want the CLI scheduler.

Use **New campaign** to enter a name, ad text, interval, and one or more groups, or select groups your personal account has already joined. New campaigns created in Telegram are disabled until you enable them. From **Campaigns**, choose one to edit the ad or interval, add or remove groups, enable or disable it, try **Send now**, or delete it with confirmation. **Next sends** shows the schedule in UTC, and **Account status** shows Telegram-imposed pauses. Use `/help` for equivalent commands, or `/cancel` to exit a prompt.

The bot saves changes to the same campaign file as the CLI. The runner picks them up between scheduling passes. Only your personal Telegram account posts the ads; the control bot does not need to join your groups.

## Manage and run campaigns

```sh
python -m adbot add weekend --message 'Weekend sale: 15% off!' --group @example_group --group=-1001234567890 --interval 2h
python -m adbot list
python -m adbot run
```

Use a group ID from `list-groups`, a public username or a Telegram group link (public or invite). For negative numeric IDs use `--group=ID`. Intervals accept a positive integer followed by `s`, `m`, `h` or `d`; choose an interval that matches the group's rules. Campaigns send as soon as they are first due. To change campaigns without restarting the runner, use `add` for new campaigns or `disable NAME`, `enable NAME`, or `remove NAME` (use a separate terminal). Use `add ... --disabled` to configure a campaign before enabling it. Campaigns live in `campaigns.json`; if editing by hand, stop the runner and keep the existing structure.

To try an enabled campaign once in a group where you have permission, run `python -m adbot send-now NAME` while the scheduler is stopped. This bypasses the configured interval, but not an active Telegram-imposed wait. A manual send resets the interval for each attempted group.

Schedules survive restarts, with no burst of catch-up sends after downtime. Each attempted group is recorded before sending so a crash or uncertain response does not cause an immediate duplicate. Flood waits pause all sends; slow mode pauses the affected group. Write errors are logged and not retried until the next interval. Use only one runner per data directory and Telegram account.

If Telegram restricts sending from the account, the scheduler stops sending. Once Telegram has lifted the restriction, stop the runner and change only `"restricted": true` to `"restricted": false` in `schedule.json` before starting it again.

## Deploy

Run Docker as your current user so the host data directory remains readable only by you:

```sh
docker build -t telegram-group-ads-userbot .
docker run --rm -it --user "$(id -u):$(id -g)" -v "$HOME/.config/adbot:/data" telegram-group-ads-userbot login
docker run -d --name adbot --restart unless-stopped --user "$(id -u):$(id -g)" -v "$HOME/.config/adbot:/data" telegram-group-ads-userbot run
docker logs -f adbot
```

Alternatively, after completing interactive login, create a user-level systemd unit at `~/.config/systemd/user/adbot.service`. Replace the repository path to match your checkout:

```ini
[Unit]
Description=Telegram group ads scheduler

[Service]
WorkingDirectory=%h/telegram-group-ads-userbot
ExecStart=%h/telegram-group-ads-userbot/.venv/bin/python -m adbot run
Restart=on-failure
RestartSec=10
UMask=0077

[Install]
WantedBy=default.target
```

Enable it with `systemctl --user daemon-reload` and `systemctl --user enable --now adbot`. Read logs with `journalctl --user -u adbot -f`. Don't run Docker and systemd for the same account at the same time.

## Offline tests

```sh
python -m unittest discover -s tests -v
```

Tests use fake Telegram clients and do not send any live messages.
