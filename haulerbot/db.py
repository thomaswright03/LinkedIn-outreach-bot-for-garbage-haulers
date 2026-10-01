"""SQLite storage: companies, prospects (contacts), messages, and an action log."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Prospect lifecycle. Terminal states are never contacted again.
STATUSES = (
    "needs_profile",   # contact known (e.g. from the seed workbook), LinkedIn profile not found yet
    "new",             # LinkedIn profile known, not contacted
    "invited",         # connection request sent
    "connected",       # accepted, intro not yet sent
    "messaged",        # intro sent, waiting for a reply
    "followed_up",     # single follow-up sent, waiting
    "replied",         # replied, not yet classified as interested/not
    "interested",
    "demo_booked",     # the goal: a demo on the calendar with Max
    "not_interested",
    "do_not_contact",  # opted out; never message again
    "no_reply",        # follow-up went unanswered; campaign done
)
TERMINAL = {"not_interested", "do_not_contact", "no_reply"}

PRIORITIES = ("A", "B", "C")
SOFTPAK_STATUSES = ("confirmed", "probable", "historical", "")

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,      -- normalized name, for dedupe
    website TEXT DEFAULT '',
    headquarters TEXT DEFAULT '',
    markets TEXT DEFAULT '',
    residential_service TEXT DEFAULT '', -- yes | no | ''
    est_residential_accounts TEXT DEFAULT '',
    softpak_status TEXT DEFAULT '',      -- confirmed | probable | historical | ''
    softpak_evidence TEXT DEFAULT '',
    evidence_url TEXT DEFAULT '',
    business_phone TEXT DEFAULT '',
    linkedin_url TEXT DEFAULT '',        -- company page
    mts_fit TEXT DEFAULT '',
    priority TEXT DEFAULT '',            -- A | B | C | ''
    next_action TEXT DEFAULT '',
    demo_booked INTEGER NOT NULL DEFAULT 0,
    demo_date TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prospects (
    id INTEGER PRIMARY KEY,
    profile_url TEXT UNIQUE,             -- NULL until the LinkedIn profile is found
    full_name TEXT NOT NULL,
    first_name TEXT NOT NULL,
    headline TEXT DEFAULT '',
    company TEXT DEFAULT '',
    company_id INTEGER REFERENCES companies(id),
    role TEXT DEFAULT '',                -- e.g. Owner/CEO, Sales/Growth Executive
    email TEXT DEFAULT '',
    email_verified INTEGER NOT NULL DEFAULT 0,  -- 1 only when the source says verified
    phone TEXT DEFAULT '',
    priority TEXT DEFAULT '',
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


# A first, then B, then prospects with no priority (generic search), then C.
_PRIORITY_ORDER = ("CASE priority WHEN 'A' THEN 0 WHEN 'B' THEN 1 WHEN 'C' THEN 3 "
                   "ELSE 2 END")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def _migrate(self) -> None:
        """Version 1 databases had no companies and required a profile URL on every prospect."""
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(prospects)")]
        if not cols or "company_id" in cols:
            return
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("ALTER TABLE prospects RENAME TO prospects_v1")
        self.conn.executescript(SCHEMA)
        keep = ", ".join(cols)
        self.conn.execute(f"INSERT INTO prospects ({keep}) SELECT {keep} FROM prospects_v1")
        self.conn.execute("DROP TABLE prospects_v1")
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys = ON")

    # --- companies -------------------------------------------------------

    @staticmethod
    def company_key(name: str) -> str:
        key = re.sub(r"[^a-z0-9 ]", "", name.lower().replace("&", " and "))
        key = re.sub(r"\b(inc|llc|co|corp|company|ltd|the)\b", " ", key)
        return " ".join(key.split())

    COMPANY_FIELDS = ("website", "headquarters", "markets", "residential_service",
                      "est_residential_accounts", "softpak_status", "softpak_evidence",
                      "evidence_url", "business_phone", "linkedin_url", "mts_fit", "priority",
                      "next_action", "notes")

    def upsert_company(self, name: str, **fields) -> int:
        """Insert or update a company by name. Blank values never overwrite existing data."""
        key = self.company_key(name)
        if not key:
            raise ValueError("company name is empty")
        fields = {k: str(v).strip() for k, v in fields.items()
                  if k in self.COMPANY_FIELDS and v is not None and str(v).strip()}
        row = self.conn.execute("SELECT id FROM companies WHERE name_key = ?", (key,)).fetchone()
        ts = now()
        if row:
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                self.conn.execute(f"UPDATE companies SET {sets}, updated_at = ? WHERE id = ?",
                                  (*fields.values(), ts, row[0]))
            cid = row[0]
        else:
            cols = ["name", "name_key", *fields, "created_at", "updated_at"]
            cur = self.conn.execute(
                f"INSERT INTO companies ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                (name.strip(), key, *fields.values(), ts, ts))
            cid = cur.lastrowid
        if "priority" in fields:
            self.conn.execute("UPDATE prospects SET priority = ? WHERE company_id = ?",
                              (fields["priority"], cid))
        self.conn.commit()
        return cid

    def get_company(self, company_id: int | None) -> sqlite3.Row | None:
        if company_id is None:
            return None
        return self.conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()

    def companies(self, priorities: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM companies"
        args: tuple = ()
        if priorities:
            sql += f" WHERE priority IN ({','.join('?' * len(priorities))})"
            args = tuple(priorities)
        return self.conn.execute(sql + " ORDER BY " + _PRIORITY_ORDER + ", name", args).fetchall()

    def contacts_of(self, company_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM prospects WHERE company_id = ? ORDER BY id",
                                 (company_id,)).fetchall()

    def add_contact(self, company_id: int, full_name: str, first_name: str, role: str = "",
                    profile_url: str | None = None, headline: str = "", email: str = "",
                    email_verified: bool = False, phone: str = "", location: str = "",
                    source_query: str = "seed") -> int | None:
        """Add a person at a known company. Returns None if they're already known."""
        company = self.get_company(company_id)
        if profile_url and self.by_url(profile_url):
            return None
        dupe = self.conn.execute(
            "SELECT id FROM prospects WHERE company_id = ? AND lower(full_name) = lower(?)",
            (company_id, full_name.strip())).fetchone()
        if dupe:
            return None
        ts = now()
        cur = self.conn.execute(
            """INSERT INTO prospects
               (profile_url, full_name, first_name, headline, company, company_id, role, email,
                email_verified, phone, priority, location, source_query, status,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (profile_url or None, full_name.strip(), first_name, headline, company["name"],
             company_id, role, email, int(bool(email_verified)), phone, company["priority"],
             location, source_query, "new" if profile_url else "needs_profile", ts, ts))
        self.conn.commit()
        return cur.lastrowid

    def set_profile(self, prospect_id: int, profile_url: str, headline: str = "") -> bool:
        """Attach a found LinkedIn profile. False if that profile belongs to someone else."""
        other = self.by_url(profile_url)
        if other and other["id"] != prospect_id:
            return False
        self.conn.execute(
            """UPDATE prospects SET profile_url = ?, headline = COALESCE(NULLIF(?, ''), headline),
               status = CASE WHEN status = 'needs_profile' THEN 'new' ELSE status END,
               updated_at = ? WHERE id = ?""", (profile_url, headline, now(), prospect_id))
        self.conn.commit()
        return True

    def book_demo(self, prospect_id: int, demo_date: str) -> None:
        p = self.get(prospect_id)
        self.set_status(prospect_id, "demo_booked", force=True)
        self.clear_human_flag(prospect_id)
        if p and p["company_id"]:
            self.conn.execute(
                "UPDATE companies SET demo_booked = 1, demo_date = ?, updated_at = ? WHERE id = ?",
                (demo_date, now(), p["company_id"]))
            self.conn.commit()

    # --- prospects -------------------------------------------------------

    def add_prospect(self, profile_url: str, full_name: str, first_name: str,
                     headline: str = "", company: str = "", location: str = "",
                     source_query: str = "", company_id: int | None = None,
                     role: str = "") -> int | None:
        """Insert a prospect. Returns the new id, or None if already known (dedupe)."""
        ts = now()
        c = self.get_company(company_id)
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO prospects
               (profile_url, full_name, first_name, headline, company, company_id, role,
                priority, location, source_query, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (profile_url, full_name, first_name, headline, c["name"] if c else company,
             company_id, role, c["priority"] if c else "", location, source_query, ts, ts),
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
                  AND needs_human = 0 ORDER BY {_PRIORITY_ORDER}, created_at"""
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
        # A booked demo isn't undone by a later reply being classified; only an opt-out is.
        if not force and current and current["status"] == "demo_booked" \
                and status not in ("demo_booked", "do_not_contact"):
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
