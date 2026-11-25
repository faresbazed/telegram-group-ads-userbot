import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon import types, utils

from adbot.__main__ import run_scheduler
from adbot.config import edit_campaigns, load_campaigns
from adbot.control import ControlBot, record_key
from adbot.scheduler import Schedule, Scheduler
from adbot.telegram import Group, GroupDirectory, control_settings


def message(text, *, sender=100, private=True, entities=None):
    return SimpleNamespace(sender_id=sender, is_private=private, raw_text=text,
                           message=SimpleNamespace(message=text, entities=entities), respond=AsyncMock())


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

    async def test_group_ban_in_lookup_does_not_restrict_account(self):
        from telethon import errors
        op = AsyncMock(side_effect=errors.UserBannedInChannelError(request=None))
        with self.assertRaises(errors.UserBannedInChannelError):
            await self.bot.account_lookup(op)
        self.assertFalse(self.state.restricted)

    async def test_peer_flood_in_lookup_restricts_account(self):
        from telethon import errors
        op = AsyncMock(side_effect=errors.PeerFloodError(request=None))
        with self.assertRaises(errors.PeerFloodError):
            await self.bot.account_lookup(op)
        self.assertTrue(self.state.restricted)

    async def text(self, text, *, sender=100, entities=None):
        event = message(text, sender=sender, entities=entities)
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
                         ("Replacement", "30m", ["-20", "-21"]))
        await self.text("/removegroup weekend")
        await self.text("@ads_group")
        self.assertEqual(load_campaigns(self.path)["weekend"].groups, ["-21"])
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
        self.send.assert_awaited_once_with("peer", "Sale today", formatting_entities=None, parse_mode=None)
        self.assertIn("UTC", (await self.text("/next weekend")).args[0])
        self.state.flood_until = 1100
        self.assertIn("flood wait", (await self.text("/status")).args[0])
        await self.text("/sendnow weekend")
        self.assertEqual(self.send.await_count, 1)
        self.state.restricted = True
        self.assertIn("restricted", (await self.text("/status")).args[0])

    async def test_formatting_entities_survive_capture_storage_and_preview(self):
        entities = [types.MessageEntityBold(offset=0, length=4),
                    types.MessageEntityCustomEmoji(offset=10, length=2, document_id=5901234567890)]
        await self.text("/new")
        await self.text("weekend")
        await self.text("Sale today 🎉", entities=entities)
        for line in ("2h", "@ads_group"):
            await self.text(line)
        campaign = load_campaigns(self.path)["weekend"]
        self.assertEqual(campaign.message, "Sale today 🎉")
        self.assertEqual(campaign.entities, [entity.to_dict() for entity in entities])
        preview = await self.click(f"preview:{record_key('weekend')}")
        prefix = len("Ad text exactly as it will be posted:\n\n".encode("utf-16-le")) // 2
        self.assertEqual(preview.kwargs["formatting_entities"], [
            types.MessageEntityBold(prefix, 4),
            types.MessageEntityCustomEmoji(prefix + 10, 2, 5901234567890),
        ])
        await self.click(f"text:{record_key('weekend')}")
        await self.text("Plain replacement")
        self.assertIsNone(load_campaigns(self.path)["weekend"].entities)

    async def test_preview_falls_back_to_plain_text_when_telegram_rejects_entities(self):
        from telethon import errors
        await self.text("/new")
        await self.text("weekend")
        await self.text("Sale today", entities=[types.MessageEntityBold(0, 4)])
        for line in ("2h", "@ads_group"):
            await self.text(line)
        event = callback(f"preview:{record_key('weekend')}")
        event.respond = AsyncMock(side_effect=[errors.MessageEmptyError(request=None), None])
        await self.bot.on_callback(event)
        self.assertEqual(event.respond.await_count, 2)
        self.assertEqual(event.respond.await_args.kwargs["formatting_entities"][0].length, 4)

    async def test_preview_drops_custom_emoji_but_keeps_bold_then_plain(self):
        from telethon import errors
        await self.text("/new")
        await self.text("weekend")
        await self.text("Sale \U0001F525 today", entities=[types.MessageEntityBold(0, 4),
                        types.MessageEntityCustomEmoji(5, 2, 5901234567890)])
        for line in ("2h", "@ads_group"):
            await self.text(line)
        event = callback(f"preview:{record_key('weekend')}")
        event.respond = AsyncMock(side_effect=[errors.MessageEmptyError(request=None), None])
        await self.bot.on_callback(event)
        kept = event.respond.await_args.kwargs["formatting_entities"]
        self.assertEqual([type(e).__name__ for e in kept], ["MessageEntityBold"])
        event = callback(f"preview:{record_key('weekend')}")
        event.respond = AsyncMock(side_effect=[errors.MessageEmptyError(request=None),
                                               errors.MessageEmptyError(request=None), None])
        await self.bot.on_callback(event)
        self.assertEqual(event.respond.await_count, 3)
        self.assertNotIn("formatting_entities", event.respond.await_args.kwargs)

    async def test_details_view_carries_shifted_entities(self):
        await self.text("/new")
        await self.text("weekend")
        await self.text("Sale today", entities=[types.MessageEntityBold(0, 4)])
        for line in ("2h", "@ads_group"):
            await self.text(line)
        view = self.bot.details(load_campaigns(self.path)["weekend"])
        ents = view.entities
        self.assertEqual(len(ents), 1)
        raw = view.text.encode("utf-16-le")
        start = ents[0]["offset"]
        self.assertEqual(raw[start * 2:(start + 4) * 2].decode("utf-16-le"), "Sale")

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

    async def test_add_group_stores_marked_ids_without_touching_other_chats(self):
        await self.create()
        invite = "https://t.me/+AdsInvite"
        untouched = ["https://t.me/another", "-21"]
        for saved, addition in (
            (invite, "-20"),
            (invite, "@ads_group"),
            ("@ads_group", "-20"),
            ("-20", invite),
        ):
            with self.subTest(saved=saved, addition=addition):
                with edit_campaigns(self.path) as campaigns:
                    campaigns["weekend"] = replace(campaigns["weekend"], groups=[saved, *untouched])
                await self.text("/addgroup weekend")
                await self.text(addition)
                self.assertEqual(load_campaigns(self.path)["weekend"].groups, ["-20", *untouched])

    async def test_bare_group_id_cannot_replace_a_resolved_peer_when_it_becomes_ambiguous(self):
        await self.create()
        directory = GroupDirectory(Mock())
        directory.expires = float("inf")
        self.scheduler.directory = directory
        bare = 12345
        marked = utils.get_peer_id(types.PeerChannel(bare))
        channel = Group(marked, "Supergroup", (), types.InputPeerChannel(bare, 1))
        directory.groups[marked] = channel
        with edit_campaigns(self.path) as campaigns:
            campaigns["weekend"] = replace(campaigns["weekend"], groups=[f"https://t.me/c/{bare}/1"])

        await self.text("/addgroup weekend")
        await self.text(str(bare))
        self.assertEqual(load_campaigns(self.path)["weekend"].groups, [str(marked)])

        directory.groups[-bare] = Group(-bare, "Basic group", (), types.InputPeerChat(bare))
        directory.lookups.clear()
        self.assertIsNone(await directory.resolve(str(bare)))
        await self.text("/addgroup weekend")
        await self.text(str(bare))
        self.assertEqual(load_campaigns(self.path)["weekend"].groups, [str(marked), str(bare)])
        self.assertEqual(await directory.resolve(str(marked)), channel)

    async def test_group_picker_callbacks_cannot_edit_a_new_flow(self):
        first = await self.create()
        with edit_campaigns(self.path) as campaigns:
            campaigns["weekday"] = replace(first, name="weekday")
        prompt = await self.text("/addgroup weekend")
        picker = await self.click(button_data(prompt.kwargs["buttons"][0][0]).decode())
        previous_pick = button_data(picker.kwargs["buttons"][1][0]).decode()
        current = await self.text("/addgroup weekday")
        self.assertIn("This menu is outdated", (await self.click(previous_pick)).args[0])
        self.assertEqual(load_campaigns(self.path)["weekday"].groups, ["-20"])
        picker = await self.click(button_data(current.kwargs["buttons"][0][0]).decode())
        await self.click(button_data(picker.kwargs["buttons"][1][0]).decode())
        self.assertEqual(load_campaigns(self.path)["weekday"].groups, ["-20", "-21"])

        prompt = await self.text("/removegroup weekend")
        picker = await self.click(button_data(prompt.kwargs["buttons"][0][0]).decode())
        previous_remove = button_data(picker.kwargs["buttons"][0][0]).decode()
        current = await self.text("/removegroup weekday")
        self.assertIn("This menu is outdated", (await self.click(previous_remove)).args[0])
        self.assertEqual(load_campaigns(self.path)["weekday"].groups, ["-20", "-21"])
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


ARABIC = __import__("re").compile("[\u0600-\u06ff]")


def labels(response):
    return [item.text for row in response.kwargs.get("buttons") or [] for item in row]


class ArabicAndPauseTests(unittest.IsolatedAsyncioTestCase):
    text = ControlTests.text
    click = ControlTests.click
    create = ControlTests.create

    def setUp(self):
        ControlTests.setUp(self)
        self.bot = ControlBot(self.path, self.scheduler, asyncio.Lock(), frozenset({100, 200}), lang="ar")

    async def test_screens_and_buttons_are_arabic(self):
        await self.create()
        key = record_key("weekend")
        screens = [await self.text("/start"), await self.text("/help"), await self.text("/list"),
                   await self.text("/status"), await self.click(f"view:{key}"),
                   await self.click(f"preview:{key}"), await self.click(f"next:{key}"),
                   await self.click(f"delete:{key}"), await self.text("/addgroup weekend"),
                   await self.click("resume"), await self.text("/pause")]
        for response in screens:
            with self.subTest(text=response.args[0][:30]):
                self.assertRegex(response.args[0], ARABIC)
                for label in labels(response):
                    self.assertRegex(label, ARABIC)
        self.assertIn("Sale today", (await self.click(f"preview:{key}")).args[0])
        self.assertRegex((await self.text("/sendnow weekend")).args[0], ARABIC)

    async def test_pause_blocks_all_sending_until_confirmed_resume(self):
        await self.create()
        await self.text("/enable weekend")
        await self.text("/pause")
        self.assertTrue(self.state.paused)
        self.assertTrue(Schedule(self.data_dir / "schedule.json").paused)
        self.assertFalse(await self.scheduler.dispatch(list(load_campaigns(self.path).values())))
        await self.text("/sendnow weekend")
        self.send.assert_not_awaited()
        asked = await self.text("/resume")
        self.assertTrue(self.state.paused)
        self.assertIn("resume_yes:", button_data(asked.kwargs["buttons"][0][0]).decode())
        self.assertRegex((await self.click("resume_yes:forged")).args[0], ARABIC)
        self.assertTrue(self.state.paused)
        await self.click(button_data(asked.kwargs["buttons"][0][0]).decode())
        self.assertFalse(self.state.paused)
        await self.text("/sendnow weekend")
        self.send.assert_awaited_once()

    async def test_group_picker_toggles_and_marks_selection(self):
        await self.text("/new")
        for line in ("weekend", "Sale today", "2h"):
            prompt = await self.text(line)
        picker = await self.click(button_data(prompt.kwargs["buttons"][0][0]).decode())
        pick = button_data(picker.kwargs["buttons"][0][0]).decode()
        picked = await self.click(pick)
        picker = await self.click(button_data(picked.kwargs["buttons"][1][0]).decode())
        self.assertTrue(labels(picker)[0].startswith("✅"))
        await self.click(pick)
        self.assertEqual(self.bot.flows[100].groups, [])

    def test_english_stays_default_and_paused_state_is_validated(self):
        self.assertEqual(ControlBot(self.path, self.scheduler, asyncio.Lock(), frozenset({1})).lang, "en")
        (self.data_dir / "schedule.json").write_text('{"attempts":{},"slow_until":{},"flood_until":0,"paused":"yes"}')
        with self.assertRaises(ValueError):
            Schedule(self.data_dir / "schedule.json")


class FailClosedTests(unittest.IsolatedAsyncioTestCase):
    text = ControlTests.text
    click = ControlTests.click
    create = ControlTests.create

    def setUp(self):
        ControlTests.setUp(self)
        self.bot = ControlBot(self.path, self.scheduler, asyncio.Lock(), frozenset({100, 200}), lang="ar")

    async def test_old_resume_button_cannot_resume_after_a_new_pause(self):
        await self.text("/pause")
        old = button_data((await self.text("/resume")).kwargs["buttons"][0][0]).decode()
        await self.text("/pause")
        await self.click(old)
        self.assertTrue(self.state.paused)
        new = button_data((await self.text("/resume")).kwargs["buttons"][0][0]).decode()
        await self.click(new)
        self.assertFalse(self.state.paused)
        await self.click(new)
        self.assertFalse(self.state.paused)

    async def test_pause_survives_restart_and_other_owners_cannot_bypass_it(self):
        await self.create()
        await self.text("/enable weekend")
        await self.text("/pause", sender=200)
        restarted = Scheduler(self.scheduler.directory, Schedule(self.data_dir / "schedule.json"),
                              clock=lambda: 1000, pause=AsyncMock())
        self.assertFalse(await restarted.dispatch(list(load_campaigns(self.path).values())))
        with self.assertRaises(ValueError):
            await restarted.dispatch(list(load_campaigns(self.path).values()), force=True)
        self.send.assert_not_awaited()

    async def test_only_selected_groups_are_saved(self):
        await self.text("/new")
        for line in ("weekend", "Sale today", "2h"):
            prompt = await self.text(line)
        picker = await self.click(button_data(prompt.kwargs["buttons"][0][0]).decode())
        by_label = {item.text: button_data(item).decode() for row in picker.kwargs["buttons"] for item in row}
        first = next(data for label, data in by_label.items() if "(-20)" in label)
        picked = await self.click(first)
        save = next(button_data(item).decode() for row in picked.kwargs["buttons"] for item in row
                    if button_data(item).startswith(b"save_groups"))
        await self.click(save)
        campaign = load_campaigns(self.path)["weekend"]
        self.assertEqual(campaign.groups, ["-20"])
        self.assertFalse(campaign.enabled)
        self.send.assert_not_awaited()

    async def test_corrupt_schedule_state_refuses_to_start(self):
        (self.data_dir / "schedule.json").write_text("{")
        with self.assertRaises(ValueError):
            Schedule(self.data_dir / "schedule.json")
