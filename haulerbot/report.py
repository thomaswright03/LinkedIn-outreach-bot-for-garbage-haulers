"""CSV export and the terminal dashboard (build step 8)."""

from __future__ import annotations

import csv
from pathlib import Path

from .db import DB, STATUSES
from .ratelimit import Throttle

EXPORT_COLUMNS = ("id", "full_name", "headline", "company", "location", "profile_url",
                  "status", "needs_human", "needs_human_reason", "last_contacted_at",
                  "last_reply", "source_query", "created_at")


def export_csv(db: DB, path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = db.all_prospects()
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EXPORT_COLUMNS)
        w.writeheader()
        for p in rows:
            inbound = [m for m in db.messages_for(p["id"]) if m["direction"] == "in"]
            row = {k: p[k] for k in EXPORT_COLUMNS if k in p.keys()}
            row["last_reply"] = inbound[-1]["body"] if inbound else ""
            w.writerow(row)
    return len(rows)


def dashboard(db: DB, throttle: Throttle) -> str:
    counts = db.status_counts()
    lines = ["Campaign" + ("  [PAUSED]" if db.paused else ""), ""]
    for s in STATUSES:
        lines.append(f"  {s:<16}{counts.get(s, 0):>5}")
    lines.append(f"  {'total':<16}{sum(counts.values()):>5}")
    lines += ["", "Last 24 hours",
              f"  invites   {throttle.used_today('invite'):>3} / {throttle.settings.max_invites_per_day}",
              f"  messages  {throttle.used_today('message'):>3} / {throttle.settings.max_messages_per_day}"]
    drafts = db.messages_in_state("draft")
    flagged = db.flagged()
    lines += ["", f"Drafts waiting for your approval: {len(drafts)}  (haulerbot review)"]
    lines.append(f"Conversations that need you: {len(flagged)}")
    for p in flagged:
        lines.append(f"  #{p['id']} {p['full_name']} ({p['company'] or p['headline']}): "
                     f"{p['needs_human_reason']}")
    return "\n".join(lines)
