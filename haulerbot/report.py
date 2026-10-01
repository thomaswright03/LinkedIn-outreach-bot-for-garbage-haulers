"""CSV export and the terminal dashboard (build step 8)."""

from __future__ import annotations

import csv
from pathlib import Path

from .db import DB, STATUSES
from .ratelimit import Throttle

EXPORT_COLUMNS = ("id", "full_name", "role", "headline", "company", "priority", "location",
                  "profile_url", "email", "email_status", "phone", "status", "needs_human",
                  "needs_human_reason", "last_contacted_at", "last_reply", "source_query",
                  "created_at")

# The master database layout from the Soft-Pak plan: one row per company.
ROLE_ORDER = ("Owner/CEO", "President", "COO", "GM/Regional President",
              "Sales/Growth Executive", "Marketing Executive", "IT/CIO")
COMPANY_COLUMNS = ("Company", "Website", "Headquarters", "States/Markets", "Residential Service",
                   "Estimated Residential Accounts", "Soft-Pak Status", "Soft-Pak Evidence",
                   "Evidence URL", *ROLE_ORDER, "Other Contacts", "LinkedIn", "Business Email",
                   "Business Phone", "MTS Fit", "Priority", "Outreach Status", "Last Touch",
                   "Next Action", "Demo Booked", "Demo Date")

# Furthest-along status wins when summarizing a company's contacts.
_PROGRESS = ("demo_booked", "interested", "replied", "followed_up", "messaged", "connected",
             "invited", "new", "needs_profile", "no_reply", "not_interested", "do_not_contact")


def email_status(p) -> str:
    if not p["email"]:
        return ""
    return "verified" if p["email_verified"] else "unverified"


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
            row["email_status"] = email_status(p)
            w.writerow(row)
    return len(rows)


def _person(p) -> str:
    return f"{p['full_name']} ({p['role'] or p['headline'] or 'contact'})"


def export_companies_csv(db: DB, path: str | Path) -> int:
    """One row per company in the master-database layout, contacts grouped by role."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    companies = db.companies()
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COMPANY_COLUMNS)
        w.writeheader()
        for c in companies:
            contacts = db.contacts_of(c["id"])
            row = {
                "Company": c["name"], "Website": c["website"], "Headquarters": c["headquarters"],
                "States/Markets": c["markets"], "Residential Service": c["residential_service"],
                "Estimated Residential Accounts": c["est_residential_accounts"],
                "Soft-Pak Status": c["softpak_status"], "Soft-Pak Evidence": c["softpak_evidence"],
                "Evidence URL": c["evidence_url"], "Business Phone": c["business_phone"],
                "MTS Fit": c["mts_fit"], "Priority": c["priority"], "Next Action": c["next_action"],
                "Demo Booked": "Yes" if c["demo_booked"] else "No", "Demo Date": c["demo_date"],
            }
            other = []
            for p in contacts:
                if p["role"] in ROLE_ORDER and not row.get(p["role"]):
                    row[p["role"]] = p["full_name"]
                else:
                    other.append(_person(p))
            row["Other Contacts"] = "; ".join(other)
            row["LinkedIn"] = "; ".join(f"{p['full_name']}: {p['profile_url']}"
                                        for p in contacts if p["profile_url"])
            # Guessed emails are labelled as such; nothing is presented as verified unless it is.
            row["Business Email"] = "; ".join(f"{p['email']} ({email_status(p)})"
                                              for p in contacts if p["email"])
            statuses = [p["status"] for p in contacts]
            row["Outreach Status"] = next((s for s in _PROGRESS if s in statuses), "")
            touches = [p["last_contacted_at"] for p in contacts if p["last_contacted_at"]]
            row["Last Touch"] = max(touches) if touches else ""
            w.writerow(row)
    return len(companies)


def dashboard(db: DB, throttle: Throttle) -> str:
    counts = db.status_counts()
    lines = ["Campaign" + ("  [PAUSED]" if db.paused else ""), ""]
    for s in STATUSES:
        lines.append(f"  {s:<16}{counts.get(s, 0):>5}")
    lines.append(f"  {'total':<16}{sum(counts.values()):>5}")
    companies = db.companies()
    if companies:
        by_tier = {t: sum(1 for c in companies if c["priority"] == t) for t in ("A", "B", "C")}
        demos = sum(1 for c in companies if c["demo_booked"])
        lines += ["", f"Seeded companies: {len(companies)}  (A {by_tier['A']}, B {by_tier['B']}, "
                      f"C {by_tier['C']})   demos booked: {demos}",
                  f"Contacting tiers: {', '.join(throttle.settings.contact_priorities) or 'none'}"]
    lines += ["", "Last 24 hours",
              f"  invites   {throttle.used_today('invite'):>3} / {throttle.settings.max_invites_per_day}",
              f"  messages  {throttle.used_today('message'):>3} / {throttle.settings.max_messages_per_day}",
              f"  searches  {throttle.used_today('search'):>3} / {throttle.settings.max_searches_per_day}"]
    drafts = db.messages_in_state("draft")
    flagged = db.flagged()
    lines += ["", f"Drafts waiting for your approval: {len(drafts)}  (haulerbot review)"]
    lines.append(f"Conversations that need you: {len(flagged)}")
    for p in flagged:
        lines.append(f"  #{p['id']} {p['full_name']} ({p['company'] or p['headline']}): "
                     f"{p['needs_human_reason']}")
    return "\n".join(lines)
