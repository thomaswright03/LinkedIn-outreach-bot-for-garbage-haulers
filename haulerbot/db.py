"""SQLite storage: prospects, messages, and an action log used for rate limiting."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Prospect lifecycle. Terminal states are never contacted again.
STATUSES = (
    "new",             # found in search, not contacted
    "invited",         # connection request sent
    "connected",       # accepted, intro not yet sent
    "messaged",        # intro sent, waiting for a reply
    "followed_up",     # single follow-up sent, waiting
    "replied",         # replied, not yet classified as interested/not
    "interested",
    "not_interested",
    "do_not_contact",  # opted out; never message again
    "no_reply",        # follow-up went unanswered; campaign done
)
TERMINAL = {"not_interested", "do_not_contact", "no_reply"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS prospects (
    id INTEGER PRIMARY KEY,
    profile_url TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    first_name TEXT NOT NULL,
    headline TEXT DEFAULT '',
    company TEXT DEFAULT '',
    location TEXT DEFAULT '',
    source_query TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    needs_human INTEGER NOT NULL DEFAULT 0,
    needs_human_reason TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_contacted_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    prospect_id INTEGER NOT NULL REFERENCES prospects(id),
    direction TEXT NOT NULL CHECK (direction IN ('out', 'in')),
    kind TEXT NOT NULL,          -- invite_note | intro | followup | reply | inbound
    body TEXT NOT NULL,
    state TEXT NOT NULL,         -- draft | approved | sent | failed | received | discarded
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,          -- invite | message
    prospect_id INTEGER,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);
CREATE INDEX IF NOT EXISTS idx_action_log_at ON action_log(at);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- prospects -------------------------------------------------------

    def add_prospect(self, profile_url: str, full_name: str, first_name: str,
                     headline: str = "", company: str = "", location: str = "",
                     source_query: str = "") -> int | None:
        """Insert a prospect. Returns the new id, or None if already known (dedupe)."""
        ts = now()
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO prospects
               (profile_url, full_name, first_name, headline, company, location,
                source_query, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (profile_url, full_name, first_name, headline, company, location,
             source_query, ts, ts),
        )
        self.conn.commit()
        return cur.lastrowid if cur.rowcount else None

    def get(self, prospect_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)).fetchone()

    def by_url(self, profile_url: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM prospects WHERE profile_url = ?", (profile_url,)).fetchone()

    def by_status(self, *statuses: str, limit: int | None = None) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        sql = f"""SELECT * FROM prospects WHERE status IN ({marks})
                  AND needs_human = 0 ORDER BY created_at"""
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql, statuses).fetchall()

    def all_prospects(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM prospects ORDER BY id").fetchall()

    def set_status(self, prospect_id: int, status: str, force: bool = False) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        current = self.get(prospect_id)
        # Opt-outs are sticky: nothing automated may move a prospect out of do_not_contact.
        # Only a manual `haulerbot set-status` (force=True) can.
        if not force and current and current["status"] == "do_not_contact" and status != "do_not_contact":
            return
        self.conn.execute("UPDATE prospects SET status = ?, updated_at = ? WHERE id = ?",
                          (status, now(), prospect_id))
        self.conn.commit()

    def flag_for_human(self, prospect_id: int, reason: str) -> None:
        self.conn.execute(
            "UPDATE prospects SET needs_human = 1, needs_human_reason = ?, updated_at = ? WHERE id = ?",
            (reason, now(), prospect_id))
        self.conn.commit()

    def clear_human_flag(self, prospect_id: int) -> None:
        self.conn.execute(
            "UPDATE prospects SET needs_human = 0, needs_human_reason = '', updated_at = ? WHERE id = ?",
            (now(), prospect_id))
        self.conn.commit()

    def flagged(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM prospects WHERE needs_human = 1 ORDER BY updated_at").fetchall()

    def touch_contacted(self, prospect_id: int) -> None:
        self.conn.execute("UPDATE prospects SET last_contacted_at = ?, updated_at = ? WHERE id = ?",
                          (now(), now(), prospect_id))
        self.conn.commit()

    # --- messages --------------------------------------------------------

    def add_message(self, prospect_id: int, direction: str, kind: str, body: str,
                    state: str, sent_at: str | None = None, error: str = "") -> int:
        cur = self.conn.execute(
            """INSERT INTO messages
               (prospect_id, direction, kind, body, state, error, created_at, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (prospect_id, direction, kind, body, state, error, now(), sent_at))
        self.conn.commit()
        return cur.lastrowid

    def update_message(self, message_id: int, *, state: str | None = None,
                       body: str | None = None, error: str | None = None,
                       sent_at: str | None = None) -> None:
        fields, vals = [], []
        for col, val in (("state", state), ("body", body), ("error", error), ("sent_at", sent_at)):
            if val is not None:
                fields.append(f"{col} = ?")
                vals.append(val)
        if not fields:
            return
        vals.append(message_id)
        self.conn.execute(f"UPDATE messages SET {', '.join(fields)} WHERE id = ?", vals)
        self.conn.commit()

    def messages_for(self, prospect_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE prospect_id = ? ORDER BY id", (prospect_id,)).fetchall()

    def messages_in_state(self, state: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT m.*, p.full_name, p.company, p.profile_url, p.status AS prospect_status
               FROM messages m JOIN prospects p ON p.id = m.prospect_id
               WHERE m.state = ? ORDER BY m.id""", (state,)).fetchall()

    def has_inbound(self, prospect_id: int, body: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM messages WHERE prospect_id = ? AND direction = 'in' AND body = ?",
            (prospect_id, body)).fetchone() is not None

    # --- rate limiting / flags ------------------------------------------

    def log_action(self, kind: str, prospect_id: int | None = None) -> None:
        self.conn.execute("INSERT INTO action_log (kind, prospect_id, at) VALUES (?, ?, ?)",
                          (kind, prospect_id, now()))
        self.conn.commit()

    def count_actions_since(self, kind: str, since_iso: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE kind = ? AND at >= ?",
            (kind, since_iso)).fetchone()[0]

    def get_kv(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self.conn.commit()

    @property
    def paused(self) -> bool:
        return self.get_kv("paused") == "1"

    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM prospects GROUP BY status").fetchall()
        return {r[0]: r[1] for r in rows}
