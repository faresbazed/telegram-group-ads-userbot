import asyncio
import logging
import math
from pathlib import Path
from time import time

from telethon import errors

from .config import Campaign, parse_interval, read_json, write_json
from .telegram import FLOOD_WAITS, GroupDirectory


log = logging.getLogger(__name__)
BETWEEN_GROUPS = 2
POLL_INTERVAL = 5
ACCOUNT_RESTRICTIONS = (errors.PeerFloodError, errors.UserBannedInChannelError)


def timestamp(value) -> bool:
    return type(value) in (int, float) and 0 <= value <= 1e100 and math.isfinite(value)


class Schedule:
    def __init__(self, path: Path):
        self.path = path
        data = read_json(path, {"attempts": {}, "slow_until": {}, "flood_until": 0})
        try:
            self.attempts: dict[str, dict[str, float]] = data["attempts"]
            self.slow_until: dict[str, float] = data["slow_until"]
            self.flood_until: float = data["flood_until"]
            self.restricted = data.get("restricted", False)
            valid = (
                isinstance(self.restricted, bool)
                and timestamp(self.flood_until)
                and isinstance(self.slow_until, dict)
                and all(timestamp(value) for value in self.slow_until.values())
                and isinstance(self.attempts, dict)
                and all(
                    isinstance(groups, dict) and all(timestamp(value) for value in groups.values())
                    for groups in self.attempts.values()
                )
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("invalid schedule.json; refusing to send without schedule state") from exc
        if not valid:
            raise ValueError("invalid schedule.json; refusing to send without schedule state")

    def save(self) -> None:
        write_json(self.path, {
            "attempts": self.attempts,
            "slow_until": self.slow_until,
            "flood_until": self.flood_until,
            "restricted": self.restricted,
        })

    def is_due(self, campaign: Campaign, group: str, now: float, force: bool = False) -> bool:
        if now < max(self.flood_until, self.slow_until.get(group, 0)):
            return False
        previous = self.attempts.get(campaign.name, {}).get(group)
        return force or previous is None or now >= previous + parse_interval(campaign.interval)


class Scheduler:
    def __init__(self, directory: GroupDirectory, state: Schedule, clock=time, pause=asyncio.sleep):
        self.directory = directory
        self.state = state
        self.clock = clock
        self.pause = pause
        self.unresolved = set()

    def flood_wait(self, seconds: int) -> None:
        self.state.flood_until = self.clock() + max(seconds, 1)
        self.state.save()
        log.warning("Telegram flood wait: pausing all sends for %d seconds", max(seconds, 1))

    def restrict_sending(self) -> None:
        self.state.restricted = True
        self.state.save()

    async def dispatch(self, campaigns: list[Campaign], *, force: bool = False) -> bool:
        if self.state.restricted:
            raise ValueError("sending is paused; review the account restriction before resuming")
        complete = True
        if self.clock() < self.state.flood_until:
            if force:
                log.warning("Cannot send during an active Telegram flood wait")
            return False
        for campaign in campaigns:
            if not campaign.enabled:
                continue
            seen = set()
            for index, target in enumerate(campaign.groups, start=1):
                reference = (campaign.name, index, target)
                try:
                    group = await self.directory.resolve(target)
                except FLOOD_WAITS as exc:
                    self.flood_wait(exc.seconds)
                    return False
                except ACCOUNT_RESTRICTIONS:
                    self.restrict_sending()
                    raise
                except errors.UnauthorizedError:
                    raise
                except (errors.RPCError, OSError) as exc:
                    log.warning("Could not look up group %d for %s (%s)", index, campaign.name, type(exc).__name__)
                    complete = False
                    continue
                if group is None:
                    if reference not in self.unresolved:
                        log.warning("Campaign %s: group %d not found among joined groups", campaign.name, index)
                        self.unresolved.add(reference)
                    complete = False
                    continue
                self.unresolved.discard(reference)
                if group.id in seen:
                    continue
                seen.add(group.id)
                key = str(group.id)
                if not self.state.is_due(campaign, key, self.clock(), force):
                    if force:
                        log.warning("Campaign %s: group %s is in slow mode", campaign.name, key)
                        complete = False
                    continue
                attempts = self.state.attempts.setdefault(campaign.name, {})
                previous = attempts.get(key)
                attempts[key] = self.clock()
                self.state.save()
                try:
                    await self.directory.client.send_message(group.peer, campaign.message, parse_mode=None)
                    log.info("Sent %s to group %s", campaign.name, key)
                except (*FLOOD_WAITS, errors.SlowModeWaitError) as exc:
                    if previous is None:
                        del attempts[key]
                    else:
                        attempts[key] = previous
                    if isinstance(exc, FLOOD_WAITS):
                        self.flood_wait(exc.seconds)
                        return False
                    self.state.slow_until[key] = self.clock() + max(exc.seconds, 1)
                    self.state.save()
                    log.warning("Group %s is in slow mode for %d seconds", key, max(exc.seconds, 1))
                    complete = False
                except errors.ChatWriteForbiddenError:
                    log.warning("Telegram forbids writing to group %s; skipping %s this interval", key, campaign.name)
                    complete = False
                except ACCOUNT_RESTRICTIONS:
                    self.restrict_sending()
                    raise
                except errors.UnauthorizedError:
                    raise
                except (errors.RPCError, OSError) as exc:
                    log.warning("Could not send %s to group %s (%s)", campaign.name, key, type(exc).__name__)
                    complete = False
                await self.pause(BETWEEN_GROUPS)
        return complete
