import argparse
import asyncio
import logging
import os
from dataclasses import replace
from pathlib import Path

from filelock import FileLock, Timeout
from telethon import errors, utils

from .config import Campaign, load_campaigns, save_campaigns
from .scheduler import ACCOUNT_RESTRICTIONS, POLL_INTERVAL, Schedule, Scheduler
from .telegram import FLOOD_WAITS, GroupDirectory, make_client


log = logging.getLogger(__name__)


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Schedule text ads in selected Telegram groups you have joined.")
    cli.add_argument("--data-dir", type=Path, default=Path.home() / ".config" / "adbot",
                     help="directory for .env, Telegram session, campaigns and schedule state")
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="sign in to your Telegram account interactively")
    commands.add_parser("list-groups", help="show joined Telegram groups and their IDs")
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


def find_campaign(campaigns: dict[str, Campaign], name: str) -> Campaign:
    try:
        return campaigns[name]
    except KeyError:
        raise ValueError(f"unknown campaign: {name}") from None


def manage(args: argparse.Namespace) -> None:
    path = args.data_dir / "campaigns.json"
    with FileLock(args.data_dir / "campaigns.lock", timeout=5, mode=0o600):
        campaigns = load_campaigns(path)
        if args.command == "list":
            if not campaigns:
                print("No campaigns configured.")
            for campaign in campaigns.values():
                status = "enabled" if campaign.enabled else "disabled"
                print(f"{campaign.name}: {status}, every {campaign.interval}, {len(campaign.groups)} group(s)")
            return
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
        save_campaigns(path, campaigns)
    print(f"{args.command}: {args.name}")


def prompt_phone() -> str:
    phone = utils.parse_phone(input("Telegram phone number (including country code): "))
    if not phone:
        raise ValueError("enter a valid Telegram phone number")
    return phone


async def telegram_command(args: argparse.Namespace) -> int:
    path = args.data_dir / "campaigns.json"
    if args.command == "send-now":
        campaign = find_campaign(load_campaigns(path), args.name)
        if reason := campaign.message_length_error():
            raise ValueError(reason)
        if not campaign.enabled:
            raise ValueError("enable the campaign before sending")
    client = make_client(args.data_dir)
    try:
        if args.command == "login":
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
        log.info("Scheduler started; campaign changes are picked up between passes")
        while True:
            campaigns = load_campaigns(path)
            await scheduler.dispatch(list(campaigns.values()))
            await asyncio.sleep(POLL_INTERVAL)
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
