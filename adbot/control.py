import asyncio
import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex

from filelock import Timeout
from telethon import Button, errors, events

from .config import (
    MAX_MESSAGE_LENGTH,
    Campaign,
    deserialize_entities,
    edit_campaigns,
    find_campaign,
    group_ref,
    load_campaigns,
    parse_interval,
    serialize_entities,
    shift_entities,
    validate_campaign_name,
)
from .scheduler import Scheduler
from .telegram import FLOOD_WAITS
from .texts import HELP_AR, translate


PAGE_SIZE = 8
ACTIONS = {
    "campaign": "view", "edit": "text", "interval": "interval",
    "addgroup": "add_group", "removegroup": "remove_group",
    "enable": "enable", "disable": "disable", "sendnow": "send", "delete": "delete", "next": "next",
    "preview": "preview",
}


def button(label: str, data: str):
    return Button.inline(label, data.encode("ascii"))


def record_key(value: str) -> str:
    return hashlib.blake2s(value.encode(), digest_size=12).hexdigest()


def utc(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OSError, OverflowError):
        return "outside the displayable date range"


def message_text(value: str) -> str:
    if not value.strip():
        raise ValueError("Campaign message must not be blank.")
    if len(value) > MAX_MESSAGE_LENGTH:
        raise ValueError(f"Campaign message exceeds Telegram's {MAX_MESSAGE_LENGTH}-character limit.")
    return value


def targets(value: str) -> list[str]:
    result = value.replace(",", " ").split()
    if not result:
        raise ValueError("Enter at least one group ID, @username, or Telegram link.")
    for target in result:
        group_ref(target)
    return result


@dataclass
class View:
    text: str
    buttons: list = field(default_factory=list)
    entities: list | None = None


@dataclass
class Flow:
    step: str
    name: str = ""
    text: str = ""
    interval: str = ""
    groups: list[str] = field(default_factory=list)
    confirmation: Campaign | None = None
    entities: list | None = None
    nonce: str = field(default_factory=lambda: token_hex(8))


class ControlBot:
    def __init__(self, path: Path, scheduler: Scheduler, dispatch_lock: asyncio.Lock, owners: frozenset[int],
                 lang: str = "en"):
        self.lang = lang
        self.path = path
        self.scheduler = scheduler
        self.dispatch_lock = dispatch_lock
        self.owners = owners
        self.flows: dict[int, Flow] = {}
        self.resume_nonce: str | None = None
        self.owner_locks = {owner: asyncio.Lock() for owner in owners}

    def register(self, bot) -> None:
        bot.add_event_handler(self.on_message, events.NewMessage(incoming=True))
        bot.add_event_handler(self.on_callback, events.CallbackQuery())

    def allowed(self, event) -> bool:
        return event.is_private and event.sender_id in self.owners

    async def on_message(self, event) -> None:
        if not self.allowed(event):
            return
        owner = event.sender_id
        message = getattr(event, "message", None)
        async with self.owner_locks[owner]:
            await self.respond(event, self.message(owner, event.raw_text, getattr(message, "entities", None)))

    async def on_callback(self, event) -> None:
        if not self.allowed(event):
            return
        await event.answer()
        owner = event.sender_id
        async with self.owner_locks[owner]:
            await self.respond(event, self.callback(owner, event.data.decode("ascii", errors="replace")))

    async def respond(self, event, operation) -> None:
        try:
            view = await operation
        except ValueError as exc:
            view = View(str(exc))
        except Timeout:
            view = View("Campaign settings are busy. Try again.")
        except errors.UserBannedInChannelError:
            view = View("Telegram refused access to that group. Other groups are not affected.")
        except errors.PeerFloodError:
            view = View("Telegram restricted the personal account. Sending is paused; see Status.")
        except FLOOD_WAITS as exc:
            view = View(f"Telegram requires waiting {exc.seconds} seconds. Do not press again until then; the bot will not retry by itself.")
        except (errors.RPCError, OSError) as exc:
            view = View(f"Could not complete that action ({type(exc).__name__}). Try again or check the runner logs.")
        for row in view.buttons:
            for item in row:
                item.text = self.t(item.text)
        text = self.t(view.text)
        entities = deserialize_entities(view.entities)
        if not entities:
            await event.respond(text, buttons=view.buttons or None, parse_mode=None, link_preview=False)
            return
        try:
            await event.respond(text, buttons=view.buttons or None, formatting_entities=entities,
                                parse_mode=None, link_preview=False)
        except errors.RPCError:
            # The control bot cannot send another account's custom emoji. Keep bold, spoiler and links;
            # only fall back to plain text if Telegram still refuses.
            kept = [e for e in entities if type(e).__name__ != "MessageEntityCustomEmoji"]
            try:
                if not kept:
                    raise errors.RPCError(None, "no entities", 400)
                await event.respond(text, buttons=view.buttons or None, formatting_entities=kept,
                                    parse_mode=None, link_preview=False)
            except errors.RPCError:
                await event.respond(text, buttons=view.buttons or None, parse_mode=None, link_preview=False)

    def t(self, text: str) -> str:
        return translate(text, self.lang)

    def menu(self) -> View:
        return View("Telegram Group Ads", [
            [button("Campaigns", "list:0"), button("New campaign", "new")],
            [button("Account status", "status"), button("Help", "help")],
            [self.pause_button()],
        ])

    def pause_button(self):
        if self.scheduler.state.paused:
            return button("Resume all sending", "resume")
        return button("Pause all sending", "pause")

    def help(self) -> View:
        if self.lang == "ar":
            return View(HELP_AR, [[button("Menu", "menu")]])
        return View(
            "/start — menu\n/list — campaigns\n/new — create a campaign\n/status — account status\n"
            "/pause — stop all sending now\n/resume — resume all sending (asks to confirm)\n"
            "/campaign NAME — manage a campaign\n/preview NAME — show the ad without sending\n/edit NAME — change ad text\n/interval NAME — change interval\n"
            "/addgroup NAME — add groups\n/removegroup NAME — remove groups\n"
            "/enable NAME or /disable NAME\n/sendnow NAME — send now\n/next NAME — next sends\n"
            "/delete NAME, then /confirm — delete\n/cancel — discard the current step",
            [[button("Menu", "menu")]],
        )

    def page(self, caption: str, entries: list, start: int, prefix: str, back=None) -> View:
        start = min(max(start, 0), max(0, (len(entries) - 1) // PAGE_SIZE * PAGE_SIZE))
        rows = [[entry] for entry in entries[start:start + PAGE_SIZE]]
        nav = []
        if start:
            nav.append(button("Previous", f"{prefix}:{start - PAGE_SIZE}"))
        if start + PAGE_SIZE < len(entries):
            nav.append(button("Next", f"{prefix}:{start + PAGE_SIZE}"))
        if nav:
            rows.append(nav)
        if back:
            rows.append(back)
        return View(caption, rows)

    def listing(self, start: int = 0) -> View:
        campaigns = load_campaigns(self.path)
        entries = [
            button(f"{'Enabled' if item.enabled else 'Disabled'}: {name}", f"view:{record_key(name)}")
            for name, item in campaigns.items()
        ]
        return self.page("Campaigns" if entries else "No campaigns configured.", entries, start, "list",
                         [button("New", "new"), button("Menu", "menu")])

    def details(self, campaign: Campaign) -> View:
        state = "Enabled" if campaign.enabled else "Disabled"
        key = record_key(campaign.name)
        rows = [
            [button("Edit ad", f"text:{key}"), button("Change interval", f"interval:{key}")],
            [button("Add groups", f"add_group:{key}"), button("Remove groups", f"remove_group:{key}")],
            [button("Disable", f"disable:{key}") if campaign.enabled else button("Enable", f"enable:{key}"),
             button("Next sends", f"next:{key}")],
            [button("Preview", f"preview:{key}"), button("Send now", f"send:{key}")],
            [button("Delete", f"delete:{key}")],
            [button("Back to campaigns", "list:0")],
        ]
        message = campaign.message[:600] + ("…" if len(campaign.message) > 600 else "")
        group_list = "\n".join(f"• \u2066{group}\u2069" for group in campaign.groups)
        group_list = group_list[:650] + ("…" if len(group_list) > 650 else "")
        t = self.t
        head = (f"{campaign.name} — {t(state)}\n{t('Every')} \u2066{campaign.interval}\u2069\n"
                f"{t('Groups')}:\n{group_list}\n\n")
        shown = campaign.entities
        if shown and len(campaign.message) > 600:
            limit = len(message[:600].encode("utf-16-le")) // 2
            shown = [e for e in shown if e["offset"] + e["length"] <= limit]
        return View(head + message, rows, entities=shift_entities(shown, head))

    def start(self, owner: int) -> View:
        self.flows[owner] = Flow("name")
        return View("Enter a new campaign name, for example weekend_sale.", [[button("Cancel", "menu")]])

    async def message(self, owner: int, text: str, entities=None) -> View:
        head, _, tail = text.strip().partition(" ")
        command = head.split("@", 1)[0].lower()
        if command in ("/start", "/cancel"):
            self.flows.pop(owner, None)
            return self.menu()
        if command == "/help":
            return self.help()
        if command == "/list":
            return self.listing()
        if command == "/status":
            return self.account_status()
        if command == "/new":
            return self.start(owner)
        if command == "/confirm":
            return self.confirm(owner)
        if command == "/pause":
            return self.pause()
        if command == "/resume":
            return self.ask_resume()
        if command.startswith("/") and command[1:] in ACTIONS:
            action = ACTIONS[command[1:]]
            name = tail.strip()
            validate_campaign_name(name)
            campaign = find_campaign(load_campaigns(self.path), name)
            return await self.action(owner, action, campaign)
        flow = self.flows.get(owner)
        if not flow:
            return self.menu()
        if flow.step == "name":
            name = text.strip()
            validate_campaign_name(name)
            if name in load_campaigns(self.path):
                raise ValueError(f"Campaign already exists: {name}")
            flow.name, flow.step = name, "new_text"
            return View("Enter the ad text to post.")
        if flow.step == "new_text":
            flow.text, flow.entities, flow.step = message_text(text), serialize_entities(entities), "new_interval"
            return View("Enter an interval, for example 30m, 2h, or 1d.")
        if flow.step == "new_interval":
            parse_interval(text)
            flow.interval, flow.step = text.strip(), "new_groups"
            return self.group_prompt(flow)
        if flow.step == "new_groups":
            return await self.create(owner, flow, flow.groups + targets(text))
        if flow.step in {"add_group", "remove_group"}:
            return await self.update_groups(owner, flow, targets(text))
        if flow.step == "text":
            changes = {"message": message_text(text), "entities": serialize_entities(entities)}
        elif flow.step == "interval":
            parse_interval(text)
            changes = {"interval": text.strip()}
        else:
            return View("Use the confirm button or /confirm to delete, or /cancel.")
        campaign = self.update(flow.name, **changes)
        self.flows.pop(owner, None)
        return self.details(campaign)

    async def callback(self, owner: int, data: str) -> View:
        action, _, key = data.partition(":")
        if action == "menu":
            self.flows.pop(owner, None)
            return self.menu()
        if action == "help":
            return self.help()
        if action == "new":
            return self.start(owner)
        if action == "status":
            return self.account_status()
        if action == "pause":
            return self.pause()
        if action == "resume":
            return self.ask_resume()
        if action == "resume_yes":
            return self.resume(key)
        if action == "confirm":
            flow = self.flows.get(owner)
            if flow is None or record_key(flow.name) != key:
                raise ValueError("That confirmation has expired. Start again from the campaign menu.")
            return self.confirm(owner)
        if action == "list":
            return self.listing(int(key))
        if action in {"groups", "pick", "save_groups", "remove_pick", "remove_list", "cancel_groups"}:
            return await self.group_callback(owner, action, key)
        for campaign in load_campaigns(self.path).values():
            if record_key(campaign.name) == key:
                return await self.action(owner, action, campaign)
        raise ValueError("That button has expired. Open /list to start again.")

    async def action(self, owner: int, action: str, campaign: Campaign) -> View:
        if action == "view":
            return self.details(campaign)
        if action == "next":
            return await self.next_sends(campaign)
        if action == "preview":
            return self.preview(campaign)
        if action in {"enable", "disable"}:
            return self.details(self.update(campaign.name, enabled=action == "enable"))
        if action in {"text", "interval", "add_group", "remove_group", "delete"}:
            flow = Flow(action, campaign.name)
            self.flows[owner] = flow
            if action == "text":
                return View("Enter the replacement ad text, or /cancel.")
            if action == "interval":
                return View("Enter the new interval, for example 30m or 2h, or /cancel.")
            if action == "delete":
                flow.confirmation = campaign
                return View(self.t(f"Delete {campaign.name}? This removes the campaign from the schedule."), [
                    [button("Confirm delete", f"confirm:{record_key(campaign.name)}"), button("Cancel", "menu")],
                ])
            return self.group_prompt(flow)
        if action == "send":
            async with self.dispatch_lock:
                current = find_campaign(load_campaigns(self.path), campaign.name)
                if reason := current.message_length_error():
                    raise ValueError(reason)
                if not current.enabled:
                    raise ValueError("Enable the campaign before sending.")
                complete = await self.scheduler.dispatch([current], force=True)
            return View(
                "Sent to all selected groups." if complete else "Some or all groups were not sent. Check Status and the runner logs.",
                [[button("Account status", "status"), button("Campaign", f"view:{record_key(campaign.name)}")]],
            )
        raise ValueError("Unknown action. Open /start to see the menu.")

    async def create(self, owner: int, flow: Flow, groups: list[str]) -> View:
        groups = await self.merge_targets([], groups)
        campaign = Campaign(flow.name, flow.text, groups, flow.interval, enabled=False, entities=flow.entities)
        with edit_campaigns(self.path, timeout=0) as campaigns:
            if campaign.name in campaigns:
                raise ValueError(f"Campaign already exists: {campaign.name}")
            campaigns[campaign.name] = campaign
        self.flows.pop(owner, None)
        return self.details(campaign)

    def update(self, name: str, **changes) -> Campaign:
        with edit_campaigns(self.path, timeout=0) as campaigns:
            campaign = replace(find_campaign(campaigns, name), **changes)
            campaigns[name] = campaign
        return campaign

    def confirm(self, owner: int) -> View:
        flow = self.flows.get(owner)
        if flow is None or flow.step != "delete":
            raise ValueError("No pending deletion.")
        with edit_campaigns(self.path, timeout=0) as campaigns:
            if find_campaign(campaigns, flow.name) != flow.confirmation:
                raise ValueError("Campaign changed. Open it again before deleting.")
            del campaigns[flow.name]
        self.flows.pop(owner, None)
        return self.listing()

    def group_prompt(self, flow: Flow) -> View:
        cancel = button("Cancel", f"cancel_groups:{flow.nonce}")
        if flow.step == "remove_group":
            return View("Enter groups to remove, or select a configured group.", [
                [button("Configured groups", f"remove_list:{flow.nonce}:0"), cancel],
            ])
        rows = [[button("Joined groups", f"groups:{flow.nonce}:0"), cancel]]
        if flow.step == "new_groups" and flow.groups:
            rows.insert(0, [button(f"Save {len(flow.groups)} selected group(s)", f"save_groups:{flow.nonce}")])
        return View("Enter group IDs, @usernames, or Telegram links (separate multiple groups with spaces).", rows)

    async def account_lookup(self, operation, *args):
        directory = self.scheduler.directory
        if wait := getattr(directory, "blocked_for", lambda: 0)():
            raise errors.FloodWaitError(request=None, capture=wait)
        try:
            return await operation(*args)
        except FLOOD_WAITS as exc:
            if hasattr(directory, "block"):
                directory.block(exc.seconds)
            raise
        except errors.PeerFloodError:
            self.scheduler.restrict_sending()
            raise

    async def target_keys(self, selected: list[str]) -> list[tuple[str, str]]:
        keys = {}
        async with self.dispatch_lock:
            for target in selected:
                ref = group_ref(target)
                if ref not in keys:
                    group = await self.account_lookup(self.scheduler.directory.resolve, target)
                    keys[ref] = ("chat", str(group.id)) if group is not None else ref
        return [keys[group_ref(target)] for target in selected]

    async def merge_targets(self, existing: list[str], additions: list[str]) -> list[str]:
        targets = existing + additions
        keys = await self.target_keys(targets)
        touched = set(keys[len(existing):])
        result = []
        seen = set()
        for target, key in zip(targets, keys):
            if key not in touched:
                result.append(target)
            elif key not in seen:
                kind, value = key
                result.append(value if kind == "chat" else target)
                seen.add(key)
        return result

    async def group_callback(self, owner: int, action: str, payload: str) -> View:
        nonce, _, key = payload.partition(":")
        flow = self.flows.get(owner)
        if flow is None or flow.nonce != nonce or flow.step not in {"new_groups", "add_group", "remove_group"}:
            raise ValueError("This menu is outdated. Open the current group picker again.")
        cancel = button("Cancel", f"cancel_groups:{nonce}")
        if action == "cancel_groups":
            self.flows.pop(owner)
            return self.menu()
        if flow.step == "remove_group":
            campaign = find_campaign(load_campaigns(self.path), flow.name)
            if action == "remove_list":
                entries = [
                    button(target[:60], f"remove_pick:{nonce}:{record_key(target)}") for target in campaign.groups
                ]
                return self.page("Choose a group to remove.", entries, int(key), f"remove_list:{nonce}", [cancel])
            if action == "remove_pick":
                selected = [target for target in campaign.groups if record_key(target) == key]
                if selected:
                    return await self.update_groups(owner, flow, selected)
                raise ValueError("That group is no longer configured for the campaign.")
            raise ValueError("This menu is outdated. Open the current group picker again.")
        if action == "save_groups" and flow.step == "new_groups":
            return await self.create(owner, flow, flow.groups)
        if action not in {"groups", "pick"}:
            raise ValueError("That group selection has expired.")
        async with self.dispatch_lock:
            groups = await self.account_lookup(self.scheduler.directory.joined, True)
        if action == "groups":
            chosen = set(flow.groups) if flow.step == "new_groups" else set()
            entries = [
                button(f"{'✅ ' if str(group.id) in chosen else ''}{group.title[:40]} ({group.id})", f"pick:{nonce}:{group.id}")
                for group in groups.values()
            ]
            back = [cancel]
            if flow.groups:
                back.insert(0, button(f"Save {len(flow.groups)} selected", f"save_groups:{nonce}"))
            caption = "Pick the groups this campaign should post in. Tap again to unselect." if entries else "No joined groups found."
            return self.page(caption, entries, int(key), f"groups:{nonce}", back)
        group_id = int(key)
        if group_id not in groups:
            raise ValueError("That group is no longer in your joined list.")
        if flow.step == "add_group":
            return await self.update_groups(owner, flow, [key])
        if key in flow.groups:
            flow.groups.remove(key)
        else:
            flow.groups.append(key)
        return self.group_prompt(flow)

    async def update_groups(self, owner: int, flow: Flow, selected: list[str]) -> View:
        current = find_campaign(load_campaigns(self.path), flow.name)
        if flow.step == "add_group":
            groups = await self.merge_targets(current.groups, selected)
        else:
            keys = await self.target_keys(current.groups + selected)
            existing = keys[:len(current.groups)]
            remove = set(keys[len(current.groups):])
            if not remove.issubset(existing):
                raise ValueError("That group is not configured for the campaign.")
            groups = [target for target, key in zip(current.groups, existing) if key not in remove]
            if not groups:
                raise ValueError("A campaign must have at least one group. Add another group or delete the campaign.")
        with edit_campaigns(self.path, timeout=0) as campaigns:
            if find_campaign(campaigns, flow.name) != current:
                raise ValueError("Campaign changed. Open it again before editing groups.")
            campaign = replace(current, groups=groups)
            campaigns[flow.name] = campaign
        self.flows.pop(owner, None)
        return self.details(campaign)

    def account_status(self) -> View:
        state = self.scheduler.state
        if state.restricted:
            status = "Sending paused: Telegram restricted the personal account."
        elif state.flood_until > self.scheduler.clock():
            status = f"Telegram flood wait: sending paused until {utc(state.flood_until)}."
        else:
            status = "No account-wide sending pause."
        t = self.t
        wait = getattr(self.scheduler.directory, "blocked_for", lambda: 0)()
        lines = [t("Sending paused. Nothing will be sent until you resume it.") if state.paused else t("Sending is on."),
                 t(status)]
        if wait:
            lines.append(t(f"Telegram asked to wait {wait} more seconds before the group list can load again."))
        for name, campaign in load_campaigns(self.path).items():
            last = max(state.attempts.get(name, {}).values(), default=None)
            lines.append(f"• {name}: {t('Enabled' if campaign.enabled else 'Disabled')}, "
                         f"{t('Last attempt')}: {utc(last) if last else t('Never sent')}")
        return View("\n".join(lines), [[button("Campaigns", "list:0"), button("Menu", "menu")], [self.pause_button()]])

    def pause(self) -> View:
        self.resume_nonce = None
        self.scheduler.state.paused = True
        self.scheduler.state.save()
        return View("Sending paused. Nothing will be sent until you resume it.", [[self.pause_button(), button("Menu", "menu")]])

    def ask_resume(self) -> View:
        if not self.scheduler.state.paused:
            return View("Sending is already on.", [[button("Menu", "menu")]])
        self.resume_nonce = token_hex(8)
        return View("Resume sending? Enabled campaigns will start posting to their selected groups on schedule.",
                    [[button("Yes, resume sending", f"resume_yes:{self.resume_nonce}"), button("Cancel", "menu")]])

    def resume(self, nonce: str) -> View:
        if not self.resume_nonce or nonce != self.resume_nonce:
            raise ValueError("That confirmation has expired. Start again from the campaign menu.")
        self.resume_nonce = None
        self.scheduler.state.paused = False
        self.scheduler.state.save()
        return View("Sending resumed.", [[self.pause_button(), button("Menu", "menu")]])

    def preview(self, campaign: Campaign) -> View:
        t = self.t
        back = [[button("Campaign", f"view:{record_key(campaign.name)}")]]
        prefix = f"{t('Ad text exactly as it will be posted:')}\n\n"
        text = (prefix + campaign.message + f"\n\n"
                f"{t('Characters')}: {len(campaign.message)} | {t('Groups count')}: {len(campaign.groups)} | "
                f"{t('Every')} \u2066{campaign.interval}\u2069\n{t('Nothing was sent.')}")
        return View(text, back, entities=shift_entities(campaign.entities, prefix))

    async def next_sends(self, campaign: Campaign) -> View:
        back = [[button("Campaign", f"view:{record_key(campaign.name)}")]]
        if not campaign.enabled:
            return View(f"{campaign.name} {self.t('is disabled.')}", back)
        if reason := campaign.message_length_error():
            return View(reason, back)
        async with self.dispatch_lock:
            state = self.scheduler.state
            now = self.scheduler.clock()
            if state.restricted or state.flood_until > now:
                return View(self.account_status().text, back)
            lines = []
            seen = set()
            for target in campaign.groups[:12]:
                group = await self.account_lookup(self.scheduler.directory.resolve, target)
                if not group:
                    lines.append(f"{target[:75]}: {self.t('not found among joined groups')}")
                elif group.id not in seen:
                    seen.add(group.id)
                    due = state.next_due(campaign, str(group.id))
                    lines.append(f"{group.title[:75]}: {self.t('due now') if due <= now else utc(due)}")
            if len(campaign.groups) > 12:
                lines.append(self.t("Showing the first 12 targets."))
        return View(f"{self.t('Next sends:')} {campaign.name}\n" + "\n".join(lines), back)
