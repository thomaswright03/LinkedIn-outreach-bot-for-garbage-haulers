from datetime import datetime

import pytest

from haulerbot.config import Settings
from haulerbot.db import DB
from haulerbot.ratelimit import Throttle


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    # Tests never call Claude; drafting falls back to templates.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, sender_name="Thomas", min_delay=0, max_delay=0)


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.sqlite3")


@pytest.fixture
def throttle(db, settings):
    # A Tuesday at 10am, so active-hours checks pass.
    return Throttle(db, settings, sleep=lambda s: None, clock=lambda: datetime(2026, 9, 29, 10, 0))


@pytest.fixture
def prospect(db):
    pid = db.add_prospect("https://www.linkedin.com/in/jane-doe/", "Jane Doe", "Jane",
                          "Owner at Peak Roll-Off", "Peak Roll-Off", "Denver, CO")
    return db.get(pid)
