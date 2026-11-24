import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile


INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
GROUP_LINK = re.compile(r"(?:https?://)?t\.me/(.+)", re.IGNORECASE)
USERNAME = re.compile(r"[a-zA-Z0-9_]+")
INVITE = re.compile(r"(?:\+|joinchat/)([a-zA-Z0-9_-]+)")
PRIVATE_LINK = re.compile(r"c/([1-9][0-9]*)(?:/[1-9][0-9]*)?")


def parse_interval(value: str) -> int:
    if not isinstance(value, str):
        raise ValueError("interval must look like 30m, 2h or 1d")
    match = re.fullmatch(r"([1-9][0-9]{0,8})([smhd])", value.strip().lower())
    if not match:
        raise ValueError("interval must be a positive whole number (up to nine digits) with s, m, h or d")
    return int(match[1]) * INTERVAL_UNITS[match[2]]


def group_ref(value: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValueError("group must be an ID, @username or t.me link")
    value = value.strip()
    if re.fullmatch(r"-?[1-9][0-9]{0,18}", value):
        return "id", value
    if value.startswith("@") and USERNAME.fullmatch(value[1:]):
        return "username", value[1:].lower()
    link = GROUP_LINK.fullmatch(value)
    if link:
        path = link[1].split("?", 1)[0].rstrip("/")
        if invite := INVITE.fullmatch(path):
            return "invite", invite[1]
        if private := PRIVATE_LINK.fullmatch(path):
            return "channel_id", private[1]
        username, sep, message_id = path.partition("/")
        if USERNAME.fullmatch(username) and (not sep or re.fullmatch(r"[1-9][0-9]*", message_id)):
            return "username", username.lower()
    raise ValueError("group must be an ID, @username or t.me link")


@dataclass(frozen=True)
class Campaign:
    name: str
    message: str
    groups: list[str]
    interval: str
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.name):
            raise ValueError("campaign name must have 1–64 letters, digits, underscores or hyphens")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("campaign message must not be blank")
        if not isinstance(self.groups, list) or not self.groups:
            raise ValueError("campaign must have at least one group")
        for group in self.groups:
            group_ref(group)
        parse_interval(self.interval)
        if not isinstance(self.enabled, bool):
            raise ValueError("campaign enabled must be true or false")


def read_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as source:
            return json.load(source)
    except FileNotFoundError:
        return default
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON in {path.name}") from exc


def write_json(path: Path, value) -> None:
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as target:
        temporary = Path(target.name)
        try:
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def load_campaigns(path: Path) -> dict[str, Campaign]:
    data = read_json(path, [])
    if not isinstance(data, list):
        raise ValueError("campaigns.json must contain a list of campaigns")
    campaigns = {}
    for entry in data:
        try:
            campaign = Campaign(**entry)
        except TypeError as exc:
            raise ValueError("invalid campaign fields in campaigns.json") from exc
        if campaign.name in campaigns:
            raise ValueError(f"duplicate campaign name: {campaign.name}")
        campaigns[campaign.name] = campaign
    return campaigns


def save_campaigns(path: Path, campaigns: dict[str, Campaign]) -> None:
    write_json(path, [asdict(campaign) for campaign in campaigns.values()])
