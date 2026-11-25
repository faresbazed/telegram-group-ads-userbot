import argparse
import asyncio
import logging
import os
from dataclasses import replace
from pathlib import Path

from getpass import getpass

from filelock import FileLock, Timeout
from telethon import errors, utils

from .config import Campaign, edit_campaigns, find_campaign, load_campaigns
from .control import ControlBot
from .scheduler import ACCOUNT_RESTRICTIONS, POLL_INTERVAL, Schedule, Scheduler
from .telegram import FLOOD_WAITS, GroupDirectory, control_language, control_settings, make_client


log = logging.getLogger(__name__)


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Schedule text ads in selected Telegram groups you have joined.")
    cli.add_argument("--data-dir", type=Path, default=Path.home() / ".config" / "adbot",
                     help="directory for .env, Telegram session, campaigns and schedule state")
    commands = cli.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="sign in to your Telegram account interactively")
    login.add_argument("--qr", action="store_true",
                       help="sign in by scanning a QR code with an already signed-in Telegram app instead of a login code")
    commands.add_parser("list-groups", help="show joined Telegram groups and their IDs")
    commands.add_parser("whoami", help="show the logged-in Telegram user ID")
    add = commands.add_parser("add", help="add a campaign")
    add.add_argument("name")
    add.add_argument("--message", required=True)
    add.add_argument("--group", action="append", required=True, help="group ID, @username or t.me link; repeat for multiple groups")
    add.add_argument("--interval", required=True, help="for example 30m, 2h or 1d")
    add.add_argument("--disabled", action="store_true", help="require enable before scheduling")
    commands.add_parser("list", help="list configured campaigns")
    for command, help_text in (
        ("enable", "enable a campaign"),
        ("disable", "pause a campaign"),
        ("remove", "remove a campaign"),
        ("send-now", "send an enabled campaign once while the scheduler is stopped"),
    ):
        commands.add_parser(command, help=help_text).add_argument("name")
    commands.add_parser("run", help="run the scheduler in the foreground")
    return cli


def manage(args: argparse.Namespace) -> None:
    path = args.data_dir / "campaigns.json"
    if args.command == "list":
        campaigns = load_campaigns(path)
        if not campaigns:
            print("No campaigns configured.")
        for campaign in campaigns.values():
            status = "enabled" if campaign.enabled else "disabled"
            print(f"{campaign.name}: {status}, every {campaign.interval}, {len(campaign.groups)} group(s)")
        return
    with edit_campaigns(path) as campaigns:
        if args.command == "add":
            if args.name in campaigns:
                raise ValueError(f"campaign already exists: {args.name}")
            campaign = Campaign(args.name, args.message, args.group, args.interval, not args.disabled)
            if reason := campaign.message_length_error():
                raise ValueError(reason)
            campaigns[args.name] = campaign
        else:
            campaign = find_campaign(campaigns, args.name)
            if args.command == "remove":
                del campaigns[args.name]
            else:
                campaigns[args.name] = replace(campaign, enabled=args.command == "enable")
    print(f"{args.command}: {args.name}")


def prompt_phone() -> str:
    phone = utils.parse_phone(os.environ.get("TELEGRAM_PHONE") or input("Telegram phone number (including country code): "))
    if not phone:
        raise ValueError("enter a valid Telegram phone number")
    return phone


QR_ATTEMPTS = 10


def show_qr(url: str) -> None:
    """Draw the QR with explicit black on white colors so it scans on any terminal theme."""
    import qrcode

    code = qrcode.QRCode(border=2)
    code.add_data(url)
    code.make(fit=True)
    rows = code.get_matrix()
    if len(rows) % 2:
        rows.append([False] * len(rows[0]))
    for top, bottom in zip(rows[::2], rows[1::2]):
        line = "".join(f"\x1b[{30 if t else 97};{40 if b else 107}m\u2580" for t, b in zip(top, bottom))
        print(line + "\x1b[0m")


async def qr_login(client) -> None:
    """Sign in by QR code. The login URL is a credential: it is only drawn as a QR, never printed or logged."""
    await client.connect()
    if await client.is_user_authorized():
        return
    qr = await client.qr_login()
    for attempt in range(QR_ATTEMPTS):
        print("Scan this QR code from a Telegram app that is already signed in:")
        print("Settings > Devices > Link Desktop Device. The code changes about every 30 seconds.")
        show_qr(qr.url)
        try:
            await qr.wait()
            return
        except asyncio.TimeoutError:
            if attempt + 1 == QR_ATTEMPTS:
                break
            await qr.recreate()
            print("The QR code expired; showing a new one.")
        except errors.SessionPasswordNeededError:
            for _ in range(3):
                try:
                    await client.sign_in(password=getpass("Two-step verification password: "))
                    return
                except errors.PasswordHashInvalidError:
                    print("Wrong password. Try again.")
            raise ValueError("two-step verification password was not accepted")
    raise ValueError("QR login timed out. Run the same command again (installer: ./install.sh --qr-login)")


async def run_scheduler(args: argparse.Namespace, scheduler: Scheduler, path: Path, settings) -> None:
    dispatch_lock = asyncio.Lock()
    bot = None
    disconnected = None
    try:
        if settings:
            token, owners = settings
            bot = make_client(args.data_dir, bot=True)
            ControlBot(path, scheduler, dispatch_lock, owners, lang=control_language(args.data_dir)).register(bot)
            await bot.start(bot_token=token)
            disconnected = bot.disconnected
            log.info("Telegram control bot started")
        log.info("Scheduler started; campaign changes are picked up between passes")
        while True:
            if disconnected is not None and disconnected.done():
                await disconnected
                raise ValueError("Telegram control bot disconnected")
            async with dispatch_lock:
                if bot is None or not scheduler.state.restricted:
                    try:
                        await scheduler.dispatch(list(load_campaigns(path).values()))
                    except ACCOUNT_RESTRICTIONS:
                        if bot is None:
                            raise
                        log.error("Telegram restricted the account; sending is paused but control bot remains available")
            await asyncio.sleep(POLL_INTERVAL)
    finally:
        if bot is not None:
            await bot.disconnect()


async def telegram_command(args: argparse.Namespace) -> int:
    path = args.data_dir / "campaigns.json"
    settings = control_settings(args.data_dir) if args.command == "run" else None
    if args.command == "send-now":
        campaign = find_campaign(load_campaigns(path), args.name)
        if reason := campaign.message_length_error():
            raise ValueError(reason)
        if not campaign.enabled:
            raise ValueError("enable the campaign before sending")
    client = make_client(args.data_dir)
    try:
        if args.command == "login":
            if args.qr:
                await qr_login(client)
            else:
                await client.start(phone=prompt_phone)
        else:
            await client.connect()
        account = await client.get_me()
        if account is None:
            raise ValueError("no authorized session; run login first")
        if account.bot:
            raise ValueError("log in with your own Telegram user account")
        if args.command == "login":
            print("Telegram session saved.")
            return 0
        if args.command == "whoami":
            print(account.id)
            return 0
        directory = GroupDirectory(client)
        if args.command == "list-groups":
            await directory.refresh()
            if not directory.groups:
                print("No joined groups found.")
            for group in directory.groups.values():
                username = f" (@{group.usernames[0]})" if group.usernames else ""
                print(f"{group.id}\t{group.title}{username}")
            return 0
        scheduler = Scheduler(directory, Schedule(args.data_dir / "schedule.json"))
        if args.command == "send-now":
            return 0 if await scheduler.dispatch([campaign], force=True) else 1
        await run_scheduler(args, scheduler, path, settings)
        return 0
    finally:
        await client.disconnect()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    os.umask(0o077)
    try:
        args.data_dir = args.data_dir.expanduser()
        args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.command in {"add", "list", "remove", "enable", "disable"}:
            manage(args)
            return 0
        with FileLock(args.data_dir / "telegram.lock", timeout=0, mode=0o600):
            return asyncio.run(telegram_command(args))
    except KeyboardInterrupt:
        return 130
    except Timeout:
        log.error("Data directory is busy; stop the other adbot command or runner and try again")
    except ACCOUNT_RESTRICTIONS:
        log.error("Telegram restricted sending from this account. Stop and review the account's restrictions.")
    except FLOOD_WAITS as exc:
        log.error("Telegram requires a wait of %d seconds before retrying", exc.seconds)
    except errors.RPCError as exc:
        log.error("Telegram command failed (%s)", type(exc).__name__)
    except EOFError:
        log.error("Telegram login requires an interactive terminal")
    except (ValueError, OSError) as exc:
        log.error("%s", exc)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
