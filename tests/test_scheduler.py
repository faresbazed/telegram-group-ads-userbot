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
            call("peer1", "<sale>", parse_mode=None), call("peer2", "<sale>", parse_mode=None),
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

    async def test_public_group_sending_ban_pauses_all_campaigns(self):
        self.send.side_effect = errors.UserBannedInChannelError(request=None)
        with self.assertRaises(errors.UserBannedInChannelError):
            await self.scheduler().dispatch([self.campaign])
        self.assertTrue(Schedule(self.path).restricted)
        another = Campaign("another", "announcement", ["@two"], "30m")
        with self.assertRaisesRegex(ValueError, "sending is paused"):
            await self.scheduler().dispatch([another], force=True)
        self.assertEqual(self.send.await_count, 1)

    def test_corrupted_state_is_not_treated_as_fresh(self):
        write_json(self.path, {"attempts": [], "slow_until": {}, "flood_until": 0})
        with self.assertRaisesRegex(ValueError, "refusing to send"):
            Schedule(self.path)
