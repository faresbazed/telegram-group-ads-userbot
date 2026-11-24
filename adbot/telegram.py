import os
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from dotenv import dotenv_values
from telethon import TelegramClient, errors, functions, types, utils

from .config import group_ref


FLOOD_WAITS = (errors.FloodWaitError, errors.FloodPremiumWaitError, errors.FloodTestPhoneWaitError)


@dataclass(frozen=True)
class Group:
    id: int
    title: str
    usernames: tuple[str, ...]
    peer: types.TypeInputPeer


def make_client(data_dir: Path) -> TelegramClient:
    dotenv = dotenv_values(data_dir / ".env", interpolate=False)
    api_id = os.environ.get("TELEGRAM_API_ID", dotenv.get("TELEGRAM_API_ID"))
    api_hash = os.environ.get("TELEGRAM_API_HASH", dotenv.get("TELEGRAM_API_HASH"))
    if not api_id or not api_id.isascii() or not api_id.isdecimal() or not int(api_id) or not api_hash:
        raise ValueError("set TELEGRAM_API_ID and TELEGRAM_API_HASH in the environment or the data directory's .env")
    return TelegramClient(
        str(data_dir / "account"), int(api_id), api_hash,
        flood_sleep_threshold=0, raise_last_call_error=True,
    )


class GroupDirectory:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.groups: dict[int, Group] = {}
        self.lookups: dict[tuple[str, str], Group | None] = {}
        self.expires = 0.0

    async def refresh(self) -> None:
        groups = {}
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            if not dialog.is_group or not isinstance(entity, (types.Chat, types.Channel)):
                continue
            if any(getattr(entity, flag, False) for flag in ("left", "kicked", "deactivated", "migrated_to")):
                continue
            usernames = [username.username.lower() for username in getattr(entity, "usernames", []) or [] if username.active]
            if username := getattr(entity, "username", None):
                usernames.insert(0, username.lower())
            groups[dialog.id] = Group(dialog.id, dialog.name, tuple(usernames), dialog.input_entity)
        self.groups = groups
        self.lookups.clear()
        self.expires = monotonic() + 60

    async def resolve(self, target: str) -> Group | None:
        if monotonic() >= self.expires:
            await self.refresh()
        reference = group_ref(target)
        if reference not in self.lookups:
            self.lookups[reference] = await self.lookup(*reference)
        return self.lookups[reference]

    async def lookup(self, kind: str, value: str) -> Group | None:
        if kind in ("id", "channel_id"):
            peer_id = int(value)
            if kind == "channel_id":
                return self.groups.get(utils.get_peer_id(types.PeerChannel(peer_id)))
            if peer_id < 0:
                return self.groups.get(peer_id)
            matches = [group for group in self.groups.values() if utils.resolve_id(group.id)[0] == peer_id]
            return matches[0] if len(matches) == 1 else None
        if kind == "username":
            return next((group for group in self.groups.values() if value in group.usernames), None)
        try:
            invitation = await self.client(functions.messages.CheckChatInviteRequest(value))
        except (errors.InviteHashEmptyError, errors.InviteHashInvalidError, errors.InviteHashExpiredError):
            return None
        if isinstance(invitation, types.ChatInviteAlready):
            return self.groups.get(utils.get_peer_id(invitation.chat))
        return None
