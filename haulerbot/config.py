"""Settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader so we don't need python-dotenv. Existing env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value and key not in os.environ:
            os.environ[key] = value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _hours(raw: str) -> tuple[int, int]:
    start, _, end = raw.partition("-")
    return int(start), int(end)


@dataclass
class Settings:
    data_dir: Path = Path("./data")
    sender_name: str = "Thomas"
    sender_company: str = ""
    case_study_result: str = ""
    max_invites_per_day: int = 20
    max_messages_per_day: int = 25
    min_delay: int = 45
    max_delay: int = 180
    active_hours: tuple[int, int] = (8, 18)
    followup_after_days: int = 5
    reply_mode: str = "draft"  # "draft" or "auto"
    model: str = "claude-opus-5-5"
    demo_with: str = "Max Garrett"
    booking_link: str = ""
    contact_priorities: tuple[str, ...] = ("A", "B")
    max_searches_per_day: int = 30
    generic_search: bool = True
    headless: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "haulerbot.sqlite3"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        reply_mode = os.environ.get("HAULERBOT_REPLY_MODE", "draft").strip().lower()
        if reply_mode not in ("draft", "auto"):
            raise ValueError("HAULERBOT_REPLY_MODE must be 'draft' or 'auto'")
        s = cls(
            data_dir=Path(os.environ.get("HAULERBOT_DATA_DIR", "./data")),
            sender_name=os.environ.get("HAULERBOT_SENDER_NAME", "Thomas"),
            sender_company=os.environ.get("HAULERBOT_SENDER_COMPANY", ""),
            case_study_result=os.environ.get("HAULERBOT_CASE_STUDY_RESULT", ""),
            max_invites_per_day=_int("HAULERBOT_MAX_INVITES_PER_DAY", 20),
            max_messages_per_day=_int("HAULERBOT_MAX_MESSAGES_PER_DAY", 25),
            min_delay=_int("HAULERBOT_MIN_DELAY", 45),
            max_delay=_int("HAULERBOT_MAX_DELAY", 180),
            active_hours=_hours(os.environ.get("HAULERBOT_ACTIVE_HOURS", "8-18")),
            followup_after_days=_int("HAULERBOT_FOLLOWUP_AFTER_DAYS", 5),
            reply_mode=reply_mode,
            model=os.environ.get("HAULERBOT_MODEL", "claude-opus-5-5"),
            demo_with=os.environ.get("HAULERBOT_DEMO_WITH", "Max Garrett"),
            booking_link=os.environ.get("HAULERBOT_BOOKING_LINK", ""),
            contact_priorities=tuple(
                x.strip().upper() for x in
                os.environ.get("HAULERBOT_CONTACT_PRIORITIES", "A,B").split(",") if x.strip()),
            max_searches_per_day=_int("HAULERBOT_MAX_SEARCHES_PER_DAY", 30),
            generic_search=os.environ.get("HAULERBOT_GENERIC_SEARCH", "1").lower()
            not in ("0", "false", "no"),
            headless=os.environ.get("HAULERBOT_HEADLESS", "").lower() in ("1", "true", "yes"),
        )
        if s.min_delay > s.max_delay:
            raise ValueError("HAULERBOT_MIN_DELAY must be <= HAULERBOT_MAX_DELAY")
        s.data_dir.mkdir(parents=True, exist_ok=True)
        return s
