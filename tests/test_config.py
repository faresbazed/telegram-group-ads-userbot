import tempfile
import unittest
from pathlib import Path

from adbot.config import Campaign, group_ref, load_campaigns, parse_interval, save_campaigns, write_json


class ConfigTests(unittest.TestCase):
    def test_intervals(self):
        for text, expected in [("1s", 1), ("30m", 1800), (" 2H ", 7200), ("1d", 86400)]:
            with self.subTest(text=text):
                self.assertEqual(parse_interval(text), expected)
        for text in ["0s", "-1m", "2.5h", "3w", "15", "", 15]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_interval(text)

    def test_group_formats(self):
        self.assertEqual(group_ref("-1001234567890"), ("id", "-1001234567890"))
        self.assertEqual(group_ref("@Example_Group"), ("username", "example_group"))
        self.assertEqual(group_ref("https://t.me/Example_Group"), ("username", "example_group"))
        self.assertEqual(group_ref("https://t.me/+AbC-Def"), ("invite", "AbC-Def"))
        self.assertEqual(group_ref("https://t.me/c/1234567890/9"), ("channel_id", "1234567890"))
        with self.assertRaises(ValueError):
            group_ref("https://example.org/Example_Group")

    def test_loading_keeps_oversized_campaigns(self):
        valid = Campaign("sale", "x" * 4096, ["@group"], "2h")
        too_long = {**vars(valid), "name": "oversized", "message": valid.message + "y"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaigns.json"
            write_json(path, [too_long, vars(valid)])
            campaigns = load_campaigns(path)
        self.assertEqual(campaigns["oversized"].message, too_long["message"])
        self.assertEqual(campaigns["sale"], valid)

    def test_config_round_trip_and_validation(self):
        campaign = Campaign("weekend", "Sale 🎉", ["@group", "-123"], "2h", False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaigns.json"
            self.assertEqual(load_campaigns(path), {})
            save_campaigns(path, {campaign.name: campaign})
            self.assertEqual(load_campaigns(path), {campaign.name: campaign})
            entry = vars(campaign)
            for data in ([entry, entry], [{"name": "missing_fields"}], [{**entry, "enabled": "false"}], {}):
                with self.subTest(data=data):
                    write_json(path, data)
                    with self.assertRaises(ValueError):
                        load_campaigns(path)
