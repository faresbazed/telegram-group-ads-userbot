import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import functions, types, utils

from adbot.telegram import GroupDirectory


class GroupDirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_resolution_is_limited_to_joined_groups(self):
        date = datetime.now(timezone.utc)
        group = types.Channel(id=10, title="Group", photo=types.ChatPhotoEmpty(), date=date,
                              username="Example_Group", megagroup=True, access_hash=5)
        channel = types.Channel(id=11, title="Channel", photo=types.ChatPhotoEmpty(), date=date,
                                username="example_channel", broadcast=True, access_hash=6)
        left = types.Channel(id=12, title="Left group", photo=types.ChatPhotoEmpty(), date=date,
                             username="left_group", megagroup=True, left=True, access_hash=7)

        async def dialogs():
            for entity in (group, channel, left):
                yield SimpleNamespace(entity=entity, id=utils.get_peer_id(entity),
                                      is_group=entity.megagroup, name=entity.title,
                                      input_entity=utils.get_input_peer(entity))

        client = AsyncMock(side_effect=[
            types.ChatInviteAlready(chat=group),
            types.ChatInviteAlready(chat=channel),
        ])
        client.iter_dialogs = dialogs
        directory = GroupDirectory(client)
        expected = await directory.resolve("@example_group")
        self.assertIsNotNone(expected)
        self.assertEqual(await directory.resolve(str(expected.id)), expected)
        self.assertEqual(await directory.resolve("https://t.me/c/10/1"), expected)
        self.assertEqual(await directory.resolve("https://t.me/Example_Group/1/2"), expected)
        self.assertEqual(await directory.resolve("https://t.me/c/10/1/2"), expected)
        self.assertEqual(await directory.resolve("https://t.me/+ExampleInvite"), expected)
        self.assertIsNone(await directory.resolve("@example_channel"))
        self.assertIsNone(await directory.resolve("@left_group"))
        self.assertIsNone(await directory.resolve("https://t.me/+ChannelInvite"))
        self.assertEqual(list(directory.groups), [expected.id])
        self.assertEqual(client.await_count, 2)
        self.assertTrue(all(
            isinstance(args.args[0], functions.messages.CheckChatInviteRequest)
            for args in client.await_args_list
        ))
