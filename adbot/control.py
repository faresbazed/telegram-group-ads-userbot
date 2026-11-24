import asyncio
import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from filelock import Timeout
from telethon import Button, errors, events

from .config import (
    MAX_MESSAGE_LENGTH,
    Campaign,
    edit_campaigns,
    find_campaign,
    group_ref,
    load_campaigns,
    parse_interval,
    validate_campaign_name,
)
from .scheduler import ACCOUNT_RESTRICTIONS, Scheduler
from .telegram import FLOOD_WAITS


PAGE_SIZE = 8
ACTIONS = {
    "campaign": "view", "edit": "text", "interval": "interval",
    "addgroup": "add_group", "removegroup": "remove_group",
    "enable": "enable", "disable": "disable", "sendnow": "send", "delete": "delete", "next": "next",
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


def merge_targets(existing: list[str], additions: list[str]) -> list[str]:
    result = dict((group_ref(target), target) for target in existing)
    for target in additions:
        result.setdefault(group_ref(target), target)
    return list(result.values())


@dataclass
class View:
    text: str
    buttons: list = field(default_factory=list)


@dataclass
class Flow:
    step: str
    name: str = ""
    text: str = ""
    interval: str = ""
    groups: list[str] = field(default_factory=list)
    confirmation: Campaign | None = None


class ControlBot:
    def __init__(self, path: Path, scheduler: Scheduler, dispatch_lock: asyncio.Lock, owners: frozenset[int]):
        self.path = path
        self.scheduler = scheduler
        self.dispatch_lock = dispatch_lock
        self.owners = owners
        self.flows: dict[int, Flow] = {}
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
        async with self.owner_locks[owner]:
            await self.respond(event, self.message(owner, event.raw_text))

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
        except ACCOUNT_RESTRICTIONS:
            view = View("Telegram restricted the personal account. Sending is paused; see Status.")
        except FLOOD_WAITS as exc:
            view = View(f"Telegram requires waiting {exc.seconds} seconds. See Status before retrying.")
        except (errors.RPCError, OSError) as exc:
            view = View(f"Could not complete that action ({type(exc).__name__}). Try again or check the runner logs.")
        await event.respond(view.text, buttons=view.buttons or None, parse_mode=None, link_preview=False)

    def menu(self) -> View:
        return View("Telegram Group Ads", [
            [button("Campaigns", "list:0"), button("New campaign", "new")],
            [button("Account status", "status"), button("Help", "help")],
        ])

    def help(self) -> View:
        return View(
            "/start — menu\n/list — campaigns\n/new — create a campaign\n/status — account status\n"
            "/campaign NAME — manage a campaign\n/edit NAME — change ad text\n/interval NAME — change interval\n"
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
            [button("Send now", f"send:{key}"), button("Delete", f"delete:{key}")],
            [button("Back to campaigns", "list:0")],
        ]
        message = campaign.message[:600] + ("…" if len(campaign.message) > 600 else "")
        group_list = ", ".join(campaign.groups)
        group_list = group_list[:650] + ("…" if len(group_list) > 650 else "")
        return View(f"{campaign.name} — {state}\nEvery {campaign.interval}\nGroups: {group_list}\n\n{message}", rows)

    def start(self, owner: int) -> View:
        self.flows[owner] = Flow("name")
        return View("Enter a new campaign name, for example weekend_sale.", [[button("Cancel", "menu")]])

    async def message(self, owner: int, text: str) -> View:
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
            flow.text, flow.step = message_text(text), "new_interval"
            return View("Enter an interval, for example 30m, 2h, or 1d.")
        if flow.step == "new_interval":
            parse_interval(text)
            flow.interval, flow.step = text.strip(), "new_groups"
            return self.group_prompt(flow)
        if flow.step == "new_groups":
            return self.create(owner, flow, merge_targets(flow.groups, targets(text)))
        if flow.step in {"add_group", "remove_group"}:
            return await self.update_groups(owner, flow, targets(text))
        if flow.step == "text":
            changes = {"message": message_text(text)}
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
        if action == "confirm":
            flow = self.flows.get(owner)
            if flow is None or record_key(flow.name) != key:
                raise ValueError("That confirmation has expired. Start again from the campaign menu.")
            return self.confirm(owner)
        if action == "list":
            return self.listing(int(key))
        if action in {"groups", "pick", "save_groups", "remove_pick", "remove_list"}:
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
                return View(f"Delete {campaign.name}? This removes the campaign from the schedule.", [
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

    def create(self, owner: int, flow: Flow, groups: list[str]) -> View:
        campaign = Campaign(flow.name, flow.text, groups, flow.interval, enabled=False)
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
        if flow.step == "remove_group":
            return View("Enter groups to remove, or select a configured group.", [
                [button("Configured groups", "remove_list:0"), button("Cancel", "menu")],
            ])
        rows = [[button("Joined groups", "groups:0"), button("Cancel", "menu")]]
        if flow.step == "new_groups" and flow.groups:
            rows.insert(0, [button(f"Save {len(flow.groups)} selected group(s)", "save_groups")])
        return View("Enter group IDs, @usernames, or Telegram links (separate multiple groups with spaces).", rows)

    async def account_lookup(self, operation, *args):
        if self.scheduler.clock() < self.scheduler.state.flood_until:
            raise ValueError(f"Telegram flood wait until {utc(self.scheduler.state.flood_until)}.")
        try:
            return await operation(*args)
        except FLOOD_WAITS as exc:
            self.scheduler.flood_wait(exc.seconds)
            raise
        except ACCOUNT_RESTRICTIONS:
            self.scheduler.restrict_sending()
            raise

    async def group_callback(self, owner: int, action: str, key: str) -> View:
        flow = self.flows.get(owner)
        if flow is not None and flow.step == "remove_group":
            campaign = find_campaign(load_campaigns(self.path), flow.name)
            if action == "remove_list":
                entries = [button(target[:60], f"remove_pick:{record_key(target)}") for target in campaign.groups]
                return self.page("Choose a group to remove.", entries, int(key), "remove_list", [button("Cancel", "menu")])
            if action == "remove_pick":
                selected = [target for target in campaign.groups if record_key(target) == key]
                if selected:
                    return await self.update_groups(owner, flow, selected)
                raise ValueError("That group is no longer configured for the campaign.")
        if flow is None or flow.step not in {"new_groups", "add_group"}:
            raise ValueError("Start a group selection from a campaign first.")
        if action == "save_groups" and flow.step == "new_groups":
            return self.create(owner, flow, flow.groups)
        if action not in {"groups", "pick"}:
            raise ValueError("That group selection has expired.")
        async with self.dispatch_lock:
            groups = await self.account_lookup(self.scheduler.directory.joined)
        if action == "groups":
            entries = [button(f"{group.title[:40]} ({group.id})", f"pick:{group.id}") for group in groups.values()]
            back = [button("Cancel", "menu")]
            if flow.groups:
                back.insert(0, button(f"Save {len(flow.groups)} selected", "save_groups"))
            return self.page("Joined groups" if entries else "No joined groups found.", entries, int(key), "groups", back)
        group_id = int(key)
        if group_id not in groups:
            raise ValueError("That group is no longer in your joined list.")
        if flow.step == "add_group":
            return await self.update_groups(owner, flow, [key])
        flow.groups = merge_targets(flow.groups, [key])
        return self.group_prompt(flow)

    async def update_groups(self, owner: int, flow: Flow, selected: list[str]) -> View:
        if flow.step == "add_group":
            with edit_campaigns(self.path, timeout=0) as campaigns:
                current = find_campaign(campaigns, flow.name)
                campaign = replace(current, groups=merge_targets(current.groups, selected))
                campaigns[flow.name] = campaign
        else:
            current = find_campaign(load_campaigns(self.path), flow.name)
            refs = {group_ref(target): target for target in current.groups}
            remove = set()
            for target in selected:
                ref = group_ref(target)
                if ref in refs:
                    remove.add(ref)
                    continue
                async with self.dispatch_lock:
                    group = await self.account_lookup(self.scheduler.directory.resolve, target)
                    matches = set()
                    if group:
                        for existing, group_target in refs.items():
                            match = await self.account_lookup(self.scheduler.directory.resolve, group_target)
                            if match is not None and match.id == group.id:
                                matches.add(existing)
                if not matches:
                    raise ValueError("That group is not configured for the campaign.")
                remove.update(matches)
            with edit_campaigns(self.path, timeout=0) as campaigns:
                if find_campaign(campaigns, flow.name) != current:
                    raise ValueError("Campaign changed. Open it again before removing groups.")
                remaining = [target for target in current.groups if group_ref(target) not in remove]
                if not remaining:
                    raise ValueError("A campaign must have at least one group. Add another group or delete the campaign.")
                campaign = replace(current, groups=remaining)
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
        return View(status, [[button("Campaigns", "list:0"), button("Menu", "menu")]])

    async def next_sends(self, campaign: Campaign) -> View:
        back = [[button("Campaign", f"view:{record_key(campaign.name)}")]]
        if not campaign.enabled:
            return View(f"{campaign.name} is disabled.", back)
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
                    lines.append(f"{target[:75]}: not found among joined groups")
                elif group.id not in seen:
                    seen.add(group.id)
                    due = state.next_due(campaign, str(group.id))
                    lines.append(f"{group.title[:75]}: {'due now' if due <= now else utc(due)}")
            if len(campaign.groups) > 12:
                lines.append("Showing the first 12 targets.")
        return View(f"Next sends: {campaign.name}\n" + "\n".join(lines), back)
