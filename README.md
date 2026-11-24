# Telegram group ads userbot

Schedule recurring text ads from your own Telegram account to groups you select. The userbot only posts to groups the account has joined; it does not join groups or send private messages.

**Only post where group administrators allow automated ads.** Follow the group rules and [Telegram's Terms of Service](https://telegram.org/tos). Repeated or unwanted messages can get your account restricted. Telegram may impose additional limits at any time.

## Set up

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
