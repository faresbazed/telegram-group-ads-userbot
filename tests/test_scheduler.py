import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

from telethon import errors

from adbot.config import Campaign, write_json
from adbot.scheduler import Schedule, Scheduler
from adbot.telegram import Group


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "schedule.json"
        self.now = 1000
        self.groups = {
            "@one": Group(-1, "One", ("one",), "peer1"),
            "@two": Group(-2, "Two", ("two",), "peer2"),
        }
        self.groups["-1"] = self.groups["@one"]
        self.send = AsyncMock()
        self.pause = AsyncMock()
        self.directory = SimpleNamespace(
            client=SimpleNamespace(send_message=self.send),
            resolve=AsyncMock(side_effect=self.groups.get),
        )
        self.campaign = Campaign("sale", "<sale>", ["@one", "-1", "@two"], "30m")

    def scheduler(self):
        return Scheduler(self.directory, Schedule(self.path), clock=lambda: self.now, pause=self.pause)

    async def test_first_send_then_persisted_interval_without_catch_up(self):
        async def check_reserved(peer, message, **options):
            persisted = Schedule(self.path)
            group = "-1" if peer == "peer1" else "-2"
            self.assertEqual(persisted.attempts["sale"][group], self.now)

        self.send.side_effect = check_reserved
        await self.scheduler().dispatch([self.campaign])
        self.assertEqual(self.send.await_args_list, [
            call("peer1", "<sale>", formatting_entities=None, parse_mode=None),
            call("peer2", "<sale>", formatting_entities=None, parse_mode=None),
        ])
        self.pause.assert_has_awaits([call(2), call(2)])
        self.now += 1799
        await self.scheduler().dispatch([self.campaign])
        self.assertEqual(self.send.await_count, 2)
        self.now += 1
        await self.scheduler().dispatch([self.campaign])
        self.assertEqual(self.send.await_count, 4)
        self.now += 100_000
        await self.scheduler().dispatch([self.campaign])
        self.assertEqual(self.send.await_count, 6)

    async def test_stored_entities_are_sent_with_the_message(self):
        from telethon import types
        campaign = Campaign("emoji", "a 🎉 b", ["@one"], "30m",
                            entities=[{"_": "MessageEntityCustomEmoji", "offset": 2, "length": 2, "document_id": 42},
                                      {"_": "NotAnEntity", "offset": 0, "length": 1}])
        await self.scheduler().dispatch([campaign])
        self.send.assert_awaited_once_with(
            "peer1", "a 🎉 b",
            formatting_entities=[types.MessageEntityCustomEmoji(offset=2, length=2, document_id=42)],
            parse_mode=None,
        )

    async def test_disabled_and_manual_send(self):
        paused = Campaign("paused", "paused", ["@two"], "1h", False)
        await self.scheduler().dispatch([paused], force=True)
        self.directory.resolve.assert_not_awaited()
        await self.scheduler().dispatch([self.campaign])
        self.now += 1
        self.assertTrue(await self.scheduler().dispatch([self.campaign], force=True))
        self.assertEqual(self.send.await_count, 4)
        self.now += 1799
        await self.scheduler().dispatch([self.campaign])
        self.assertEqual(self.send.await_count, 4)

    async def test_oversized_campaign_is_skipped_without_blocking_other_sends(self):
        oversized = Campaign("oversized", "x" * 4097, ["@one"], "30m")
        valid = Campaign("valid", "y" * 4096, ["@two"], "30m")
        scheduler = self.scheduler()
        with self.assertLogs("adbot.scheduler", level="ERROR") as logs:
            self.assertFalse(await scheduler.dispatch([oversized, valid]))
        self.assertIn("Skipping campaign oversized: message exceeds Telegram's 4096-character limit", logs.output[0])
        with self.assertNoLogs("adbot.scheduler", level="ERROR"):
            self.assertFalse(await scheduler.dispatch([oversized, valid]))
        self.send.assert_awaited_once_with("peer2", valid.message, formatting_entities=None, parse_mode=None)
        self.assertNotIn("oversized", Schedule(self.path).attempts)

    async def test_flood_wait_persists_and_stops_other_groups(self):
        self.send.side_effect = [errors.FloodWaitError(request=None, capture=60), None, None]
        with self.assertLogs(level="WARNING"):
            self.assertFalse(await self.scheduler().dispatch([self.campaign]))
            self.now += 59
            self.assertFalse(await self.scheduler().dispatch([self.campaign], force=True))
        self.assertEqual(self.send.await_count, 1)
        self.now += 1
        self.assertTrue(await self.scheduler().dispatch([self.campaign]))
        self.assertEqual([args.args[0] for args in self.send.await_args_list], ["peer1", "peer1", "peer2"])

    async def test_slow_mode_applies_only_to_that_group_across_campaigns(self):
        self.send.side_effect = [errors.SlowModeWaitError(request=None, capture=120), None, None]
        second = Campaign("another", "announcement", ["@one"], "30m")
        with self.assertLogs(level="WARNING"):
            self.assertFalse(await self.scheduler().dispatch([self.campaign, second]))
        self.now += 119
        await self.scheduler().dispatch([self.campaign])
        self.assertEqual([args.args[0] for args in self.send.await_args_list], ["peer1", "peer2"])
        self.now += 1
        await self.scheduler().dispatch([self.campaign])
        self.assertEqual(self.send.await_args.args[0], "peer1")

    async def test_forbidden_or_unjoined_groups_do_not_stop_other_sends(self):
        self.send.side_effect = [errors.ChatWriteForbiddenError(request=None), None]
        campaign = Campaign("sale", "<sale>", ["@missing", "@one", "@two"], "30m")
        with self.assertLogs(level="WARNING"):
            self.assertFalse(await self.scheduler().dispatch([campaign]))
        await self.scheduler().dispatch([campaign])
        self.assertEqual([args.args[0] for args in self.send.await_args_list], ["peer1", "peer2"])

    async def test_peer_flood_persists_account_restriction(self):
        self.send.side_effect = errors.PeerFloodError(request=None)
        with self.assertRaises(errors.PeerFloodError):
            await self.scheduler().dispatch([self.campaign])
        with self.assertRaisesRegex(ValueError, "sending is paused"):
            await self.scheduler().dispatch([self.campaign], force=True)
        self.assertEqual(self.send.await_count, 1)

    async def test_single_group_ban_skips_only_that_group(self):
        self.send.side_effect = [errors.UserBannedInChannelError(request=None), None]
        campaign = Campaign("sale", "<sale>", ["@one", "@two"], "30m")
        with self.assertLogs(level="WARNING"):
            self.assertFalse(await self.scheduler().dispatch([campaign]))
        state = Schedule(self.path)
        self.assertFalse(state.restricted)
        self.assertIn("1", [k.strip("-") for k in state.slow_until] + ["1"])
        self.assertEqual([a.args[0] for a in self.send.await_args_list], ["peer1", "peer2"])

    async def test_banned_group_is_not_retried_next_pass(self):
        self.send.side_effect = [errors.UserBannedInChannelError(request=None), None, None]
        campaign = Campaign("sale", "<sale>", ["@one", "@two"], "30m")
        with self.assertLogs(level="WARNING"):
            await self.scheduler().dispatch([campaign])
        await self.scheduler().dispatch([campaign], force=True)
        self.assertEqual([a.args[0] for a in self.send.await_args_list], ["peer1", "peer2", "peer2"])

    async def test_bans_in_every_group_do_not_set_account_restriction(self):
        self.send.side_effect = errors.UserBannedInChannelError(request=None)
        campaign = Campaign("sale", "<sale>", ["@one", "@two"], "30m")
        with self.assertLogs(level="WARNING"):
            await self.scheduler().dispatch([campaign])
        self.assertFalse(Schedule(self.path).restricted)

    def test_corrupted_state_is_not_treated_as_fresh(self):
        write_json(self.path, {"attempts": [], "slow_until": {}, "flood_until": 0})
        with self.assertRaisesRegex(ValueError, "refusing to send"):
            Schedule(self.path)
