"""Human-paced throttling (build step 4): daily caps, active hours, randomized gaps."""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone

from .config import Settings
from .db import DB


class Throttle:
    def __init__(self, db: DB, settings: Settings, sleep=time.sleep, clock=None):
        self.db = db
        self.settings = settings
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now().astimezone())

    def _cap(self, kind: str) -> int:
        return {"invite": self.settings.max_invites_per_day,
                "message": self.settings.max_messages_per_day,
                "search": self.settings.max_searches_per_day}[kind]

    def used_today(self, kind: str) -> int:
        # Rolling 24 hours, so a run just after midnight can't double the daily volume.
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        return self.db.count_actions_since(kind, since)

    def remaining(self, kind: str) -> int:
        return max(0, self._cap(kind) - self.used_today(kind))

    def within_active_hours(self) -> bool:
        start, end = self.settings.active_hours
        now = self._clock()
        return now.weekday() < 5 and start <= now.hour < end

    def can(self, kind: str) -> tuple[bool, str]:
        if self.db.paused:
            return False, "campaign is paused (haulerbot resume)"
        if not self.within_active_hours():
            return False, "outside active hours (weekdays, HAULERBOT_ACTIVE_HOURS)"
        if self.remaining(kind) <= 0:
            return False, f"daily {kind} cap reached ({self._cap(kind)})"
        return True, ""

    def record(self, kind: str, prospect_id: int | None) -> None:
        self.db.log_action(kind, prospect_id)

    def wait(self) -> float:
        gap = random.uniform(self.settings.min_delay, self.settings.max_delay)
        # Occasionally take a longer break, like someone stepping away from the desk.
        if random.random() < 0.1:
            gap += random.uniform(120, 480)
        self._sleep(gap)
        return gap
