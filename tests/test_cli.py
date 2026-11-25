import asyncio
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telethon import errors

from adbot.__main__ import main, prompt_phone, qr_login
from adbot.config import load_campaigns, write_json


SECRET_URL = "tg://login?token=SECRETTOKEN"


def fake_client(waits):
    qr = MagicMock(url=SECRET_URL)
    qr.wait = AsyncMock(side_effect=waits)
    qr.recreate = AsyncMock()
    client = MagicMock()
    client.connect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=False)
    client.qr_login = AsyncMock(return_value=qr)
    client.sign_in = AsyncMock()
    return client, qr


class QrLoginTests(unittest.IsolatedAsyncioTestCase):
    async def run_login(self, client):
        with patch("adbot.__main__.show_qr") as show, redirect_stdout(io.StringIO()) as output:
            await qr_login(client)
        self.assertNotIn("SECRETTOKEN", output.getvalue())
        return show

    async def test_scan_succeeds_and_url_is_only_drawn(self):
        client, qr = fake_client([object()])
        show = await self.run_login(client)
        show.assert_called_once_with(SECRET_URL)
        client.sign_in.assert_not_called()

    async def test_expired_code_is_replaced(self):
        client, qr = fake_client([asyncio.TimeoutError(), object()])
        show = await self.run_login(client)
        self.assertEqual(show.call_count, 2)
        qr.recreate.assert_awaited_once()

    async def test_two_step_password_is_read_hidden(self):
        client, qr = fake_client([errors.SessionPasswordNeededError(None)])
        with patch("adbot.__main__.getpass", return_value="pw") as ask:
            await self.run_login(client)
        ask.assert_called_once()
        client.sign_in.assert_awaited_once_with(password="pw")

    async def test_gives_up_after_repeated_expiry(self):
        client, qr = fake_client([asyncio.TimeoutError()] * 10)
        with self.assertRaises(ValueError):
            await self.run_login(client)

    async def test_existing_session_skips_qr(self):
        client, qr = fake_client([])
        client.is_user_authorized = AsyncMock(return_value=True)
        show = await self.run_login(client)
        show.assert_not_called()
        client.qr_login.assert_not_called()


class CliTests(unittest.TestCase):
    def test_installer_can_supply_a_phone_without_another_prompt(self):
        with patch.dict("os.environ", {"TELEGRAM_PHONE": "+15551234567"}), patch("builtins.input") as prompt:
            self.assertEqual(prompt_phone(), "15551234567")
            prompt.assert_not_called()

    def test_cli_can_manage_but_not_send_an_oversized_saved_campaign(self):
        with tempfile.TemporaryDirectory() as directory, patch("adbot.__main__.os.umask"):
            args = ["--data-dir", directory]
            path = Path(directory) / "campaigns.json"
            oversized = "x" * 4097
            with self.assertLogs(level="ERROR") as logs:
                self.assertEqual(main([*args, "add", "large", "--message", oversized,
                                       "--group", "@group", "--interval", "2h"]), 1)
            self.assertIn("4096-character limit", logs.output[0])
            self.assertFalse(path.exists())
            write_json(path, [{"name": "large", "message": oversized, "groups": ["@group"], "interval": "2h"}])
            with patch("adbot.__main__.make_client") as client, self.assertLogs(level="ERROR") as logs:
                self.assertEqual(main([*args, "send-now", "large"]), 1)
            self.assertIn("campaign large: message exceeds Telegram's 4096-character limit", logs.output[0])
            client.assert_not_called()
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main([*args, "list"]), 0)
                self.assertEqual(main([*args, "disable", "large"]), 0)
                self.assertIn("large: enabled", output.getvalue())
                self.assertFalse(load_campaigns(path)["large"].enabled)
                self.assertEqual(main([*args, "remove", "large"]), 0)
            self.assertEqual(load_campaigns(path), {})
