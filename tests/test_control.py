import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from adbot.__main__ import run_scheduler
from adbot.config import edit_campaigns, load_campaigns
from adbot.control import ControlBot, record_key
from adbot.scheduler import Schedule, Scheduler
from adbot.telegram import Group, control_settings


def message(text, *, sender=100, private=True):
    return SimpleNamespace(sender_id=sender, is_private=private, raw_text=text, respond=AsyncMock())


def callback(data, *, sender=100, private=True):
    return SimpleNamespace(sender_id=sender, is_private=private, data=data.encode(),
                           answer=AsyncMock(), respond=AsyncMock())


def button_data(inline_button):
    return getattr(inline_button, "type", inline_button).data


class ControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        self.path = self.data_dir / "campaigns.json"
        self.state = Schedule(self.data_dir / "schedule.json")
        self.group = Group(-20, "Approved ads", ("ads_group",), "peer")
        self.extra = Group(-21, "Another group", ("another",), "another_peer")
        self.send = AsyncMock()
        directory = SimpleNamespace(
            joined=AsyncMock(return_value={self.group.id: self.group, self.extra.id: self.extra}),
            resolve=AsyncMock(side_effect={
                "@ads_group": self.group, "-20": self.group, "https://t.me/another": self.extra, "-21": self.extra,
                "https://t.me/+AdsInvite": self.group,
            }.get),
            client=SimpleNamespace(send_message=self.send),
        )
        self.scheduler = Scheduler(directory, self.state, clock=lambda: 1000, pause=AsyncMock())
        self.bot = ControlBot(self.path, self.scheduler, asyncio.Lock(), frozenset({100, 200}))

    async def text(self, text, *, sender=100):
        event = message(text, sender=sender)
        await self.bot.on_message(event)
        event.respond.assert_awaited_once()
        return event.respond.await_args

    async def click(self, data, *, sender=100):
        event = callback(data, sender=sender)
        await self.bot.on_callback(event)
        event.answer.assert_awaited_once()
        event.respond.assert_awaited_once()
        return event.respond.await_args

    async def create(self):
        for text in ("/new", "weekend", "Sale today", "2h", "@ads_group"):
            await self.text(text)
        return load_campaigns(self.path)["weekend"]

    async def test_rejects_unauthorized_users_and_all_group_chats(self):
        bot_client = Mock()
        self.bot.register(bot_client)
        self.assertEqual(bot_client.add_event_handler.call_count, 2)
        for sender, private in ((900, True), (100, False)):
            with self.subTest(sender=sender, private=private):
                incoming = message("/new", sender=sender, private=private)
                query = callback("new", sender=sender, private=private)
                await self.bot.on_message(incoming)
                await self.bot.on_callback(query)
                incoming.respond.assert_not_awaited()
                query.respond.assert_not_awaited()
                query.answer.assert_not_awaited()
        self.assertFalse(self.bot.flows)
        self.assertIn("Telegram Group Ads", (await self.text("/start", sender=200)).args[0])

    async def test_guided_creation_uses_joined_picker_and_is_private_per_owner(self):
        menu = await self.text("/start")
        self.assertEqual(button_data(menu.kwargs["buttons"][0][1]), b"new")
        await self.click("new")
        await self.text("weekend")
        self.assertIn("4096-character limit", (await self.text("x" * 4097)).args[0])
        await self.text("Sale today")
        self.assertIn("interval must", (await self.text("bad interval")).args[0])
        prompt = await self.text("2h")
        await self.text("/cancel", sender=200)
        picker = await self.click(button_data(prompt.kwargs["buttons"][0][0]).decode())
        pick = button_data(picker.kwargs["buttons"][0][0])
        self.assertRegex(pick, rb"^pick:[a-f0-9]{16}:-20$")
        selected = await self.click(pick.decode())
        await self.click(button_data(selected.kwargs["buttons"][0][0]).decode())
        campaign = load_campaigns(self.path)["weekend"]
        self.assertEqual((campaign.message, campaign.interval, campaign.groups, campaign.enabled),
                         ("Sale today", "2h", ["-20"], False))
        self.assertNotIn(100, self.bot.flows)
        self.send.assert_not_awaited()

    async def test_edit_groups_text_interval_and_confirm_delete(self):
        await self.create()
        await self.click(f"text:{record_key('weekend')}")
        await self.text("Replacement")
        await self.text("/interval weekend")
        await self.text("30m")
        await self.text("/addgroup weekend")
        await self.text("https://t.me/another")
        campaign = load_campaigns(self.path)["weekend"]
        self.assertEqual((campaign.message, campaign.interval, campaign.groups),
                         ("Replacement", "30m", ["@ads_group", "https://t.me/another"]))
        await self.text("/removegroup weekend")
        await self.text("-20")
        self.assertEqual(load_campaigns(self.path)["weekend"].groups, ["https://t.me/another"])
        prompt = await self.text("/removegroup weekend")
        picker = await self.click(button_data(prompt.kwargs["buttons"][0][0]).decode())
        self.assertIn("at least one group", (
            await self.click(button_data(picker.kwargs["buttons"][0][0]).decode())
        ).args[0])
        await self.text("/enable weekend")
        self.assertTrue(load_campaigns(self.path)["weekend"].enabled)
        await self.text("/disable weekend")
        self.assertFalse(load_campaigns(self.path)["weekend"].enabled)
        await self.text("/delete weekend")
        self.assertIn("weekend", load_campaigns(self.path))
        await self.text("/confirm", sender=200)
        self.assertIn("weekend", load_campaigns(self.path))
        await self.click(f"confirm:{record_key('weekend')}")
        self.assertEqual(load_campaigns(self.path), {})

    async def test_send_now_uses_personal_client_and_reports_schedule(self):
        await self.create()
        self.assertIn("Enable the campaign", (await self.text("/sendnow weekend")).args[0])
        await self.text("/enable weekend")
        self.assertIn("due now", (await self.text("/next weekend")).args[0])
        self.assertIn("Sent to all", (await self.text("/sendnow weekend")).args[0])
        self.send.assert_awaited_once_with("peer", "Sale today", parse_mode=None)
        self.assertIn("UTC", (await self.text("/next weekend")).args[0])
        self.state.flood_until = 1100
        self.assertIn("flood wait", (await self.text("/status")).args[0])
        await self.text("/sendnow weekend")
        self.assertEqual(self.send.await_count, 1)
        self.state.restricted = True
        self.assertIn("restricted", (await self.text("/status")).args[0])

    async def test_group_edits_dedupe_and_remove_all_aliases_by_chat(self):
        await self.create()
        await self.text("/addgroup weekend")
        await self.text("-20 -21")
        self.assertEqual(load_campaigns(self.path)["weekend"].groups, ["-20", "-21"])

        with edit_campaigns(self.path) as campaigns:
            campaigns["weekend"] = replace(campaigns["weekend"], groups=["@ads_group", "-20", "-21"])
        await self.text("/removegroup weekend")
        await self.text("@ads_group")
        self.assertEqual(load_campaigns(self.path)["weekend"].groups, ["-21"])

    async def test_add_group_prefers_stable_refs_without_touching_other_chats(self):
        await self.create()
        invite = "https://t.me/+AdsInvite"
        untouched = ["https://t.me/another", "-21"]
        for saved, addition, chosen in (
            (invite, "-20", "-20"),
            (invite, "@ads_group", "@ads_group"),
            ("@ads_group", "-20", "-20"),
            ("-20", invite, "-20"),
        ):
            with self.subTest(saved=saved, addition=addition):
                with edit_campaigns(self.path) as campaigns:
                    campaigns["weekend"] = replace(campaigns["weekend"], groups=[saved, *untouched])
                await self.text("/addgroup weekend")
                await self.text(addition)
                self.assertEqual(load_campaigns(self.path)["weekend"].groups, [chosen, *untouched])

    async def test_group_picker_callbacks_cannot_edit_a_new_flow(self):
        first = await self.create()
        with edit_campaigns(self.path) as campaigns:
            campaigns["weekday"] = replace(first, name="weekday")
        prompt = await self.text("/addgroup weekend")
        picker = await self.click(button_data(prompt.kwargs["buttons"][0][0]).decode())
        previous_pick = button_data(picker.kwargs["buttons"][1][0]).decode()
        current = await self.text("/addgroup weekday")
        self.assertIn("This menu is outdated", (await self.click(previous_pick)).args[0])
        self.assertEqual(load_campaigns(self.path)["weekday"].groups, ["@ads_group"])
        picker = await self.click(button_data(current.kwargs["buttons"][0][0]).decode())
        await self.click(button_data(picker.kwargs["buttons"][1][0]).decode())
        self.assertEqual(load_campaigns(self.path)["weekday"].groups, ["@ads_group", "-21"])

        prompt = await self.text("/removegroup weekend")
        picker = await self.click(button_data(prompt.kwargs["buttons"][0][0]).decode())
        previous_remove = button_data(picker.kwargs["buttons"][0][0]).decode()
        current = await self.text("/removegroup weekday")
        self.assertIn("This menu is outdated", (await self.click(previous_remove)).args[0])
        self.assertEqual(load_campaigns(self.path)["weekday"].groups, ["@ads_group", "-21"])
        picker = await self.click(button_data(current.kwargs["buttons"][0][0]).decode())
        await self.click(button_data(picker.kwargs["buttons"][0][0]).decode())
        self.assertEqual(load_campaigns(self.path)["weekday"].groups, ["-21"])

    async def test_runner_registers_and_disconnects_optional_control_client(self):
        client = SimpleNamespace(
            start=AsyncMock(), disconnected=asyncio.get_running_loop().create_future(),
            disconnect=AsyncMock(), add_event_handler=Mock(),
        )
        scheduler = SimpleNamespace(state=self.state, dispatch=AsyncMock(side_effect=asyncio.CancelledError))
        args = SimpleNamespace(data_dir=self.data_dir)
        with patch("adbot.__main__.make_client", return_value=client) as factory:
            with self.assertRaises(asyncio.CancelledError):
                await run_scheduler(args, scheduler, self.path, ("test-token", frozenset({100})))
            factory.assert_called_once_with(self.data_dir, bot=True)
            client.start.assert_awaited_once_with(bot_token="test-token")
            client.disconnect.assert_awaited_once()
            self.assertEqual(client.add_event_handler.call_count, 2)
            factory.reset_mock()
            with self.assertRaises(asyncio.CancelledError):
                await run_scheduler(args, scheduler, self.path, None)
            factory.assert_not_called()

    def test_bot_configuration_requires_owner_ids_only_when_token_is_present(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(control_settings(self.data_dir))
        with patch.dict("os.environ", {"BOT_TOKEN": "test-token", "OWNER_ID": "100, 200"}, clear=True):
            self.assertEqual(control_settings(self.data_dir), ("test-token", frozenset({100, 200})))
        with patch.dict("os.environ", {"BOT_TOKEN": "test-token"}, clear=True):
            with self.assertRaisesRegex(ValueError, "OWNER_ID"):
                control_settings(self.data_dir)
