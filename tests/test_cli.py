import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from adbot.__main__ import main
from adbot.config import load_campaigns, write_json


class CliTests(unittest.TestCase):
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
