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


class FloodSafeDirectoryTests(unittest.IsolatedAsyncioTestCase):
    def make(self, iterator):
        client = SimpleNamespace(iter_dialogs=iterator)
        return GroupDirectory(client, pause=AsyncMock())

    async def test_flood_wait_blocks_retries_until_it_ends(self):
        from telethon import errors
        calls = []

        async def dialogs():
            calls.append(1)
            raise errors.FloodWaitError(request=None, capture=25)
            yield

        directory = self.make(dialogs)
        with self.assertRaises(errors.FloodWaitError):
            await directory.joined()
        self.assertGreater(directory.blocked_for(), 20)
        for _ in range(3):
            with self.assertRaises(errors.FloodWaitError) as raised:
                await directory.joined()
            self.assertLessEqual(raised.exception.seconds, 25)
        self.assertEqual(len(calls), 1)

    async def test_cache_and_single_flight(self):
        import asyncio
        calls = []
        group = types.Channel(id=10, title="G", photo=types.ChatPhotoEmpty(), date=datetime.now(timezone.utc),
                              megagroup=True, access_hash=5)

        async def dialogs():
            calls.append(1)
            await asyncio.sleep(0.01)
            yield SimpleNamespace(entity=group, id=utils.get_peer_id(group), is_group=True, name="G",
                                  input_entity=utils.get_input_peer(group))

        directory = self.make(dialogs)
        results = await asyncio.gather(*(directory.joined() for _ in range(5)))
        await directory.joined()
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(len(result) == 1 for result in results))

    async def test_stale_list_is_served_during_flood_wait(self):
        from telethon import errors

        async def dialogs():
            raise errors.FloodWaitError(request=None, capture=30)
            yield

        directory = self.make(dialogs)
        stale = {-1: object()}
        directory.groups = stale
        self.assertIs(await directory.joined(allow_stale=True), stale)
