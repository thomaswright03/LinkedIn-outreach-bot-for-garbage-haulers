"""Campaign steps that tie discovery, drafting, sending and reply handling together.

Each step takes an `actions` object (the `linkedin` module in real runs, a fake in
tests) so the logic can be exercised without a browser.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import linkedin as live_actions
from .browser import NeedsLogin
from .classify import STATUS_FOR_INTENT, classify
from .config import Settings
from .db import DB, TERMINAL, now
from .discovery import DEFAULT_QUERIES, qualifies, scrape_search_page, search_url
from .messaging import DraftRejected, draft, draft_reply, validate_outbound
from .ratelimit import Throttle


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# --- discovery ----------------------------------------------------------------

def discover(page, db: DB, queries=DEFAULT_QUERIES, pages_per_query: int = 2,
             scrape=scrape_search_page) -> dict:
    found = added = skipped = 0
    for q in queries:
        for n in range(1, pages_per_query + 1):
            for c in scrape(page, search_url(q, n)):
                found += 1
                if not qualifies(c.headline, c.company):
                    skipped += 1
                    continue
                if db.add_prospect(c.profile_url, c.full_name, c.first_name, c.headline,
                                   c.company, c.location, source_query=q):
                    added += 1
                    _log(f"new prospect: {c.full_name} | {c.headline}")
    return {"found": found, "added": added, "not_a_fit": skipped}


# --- outbound -----------------------------------------------------------------

def _ok_to_contact(db: DB, prospect_id: int) -> bool:
    p = db.get(prospect_id)
    return bool(p) and p["status"] not in TERMINAL and not p["needs_human"]


def send_invites(page, db: DB, settings: Settings, throttle: Throttle,
                 actions=live_actions, limit: int | None = None) -> int:
    sent = 0
    for p in db.by_status("new"):
        if limit is not None and sent >= limit:
            break
        ok, why = throttle.can("invite")
        if not ok:
            _log(f"stopping invites: {why}")
            break
        if not _ok_to_contact(db, p["id"]):
            continue
        try:
            note = draft("invite_note", p, settings)
            result = actions.send_connection_request(page, p["profile_url"], note)
        except NeedsLogin:
            raise
        except (DraftRejected, Exception) as exc:  # one bad profile shouldn't stop the run
            _log(f"invite failed for {p['full_name']}: {exc}")
            db.add_message(p["id"], "out", "invite_note", "", "failed", error=str(exc))
            continue
        if result == "already_connected":
            db.set_status(p["id"], "connected")
        elif result == "already_pending":
            db.set_status(p["id"], "invited")
        else:
            db.add_message(p["id"], "out", "invite_note", note, "sent", sent_at=now())
            db.set_status(p["id"], "invited")
            db.touch_contacted(p["id"])
            throttle.record("invite", p["id"])
            sent += 1
            _log(f"invited {p['full_name']} ({p['company'] or p['headline']})")
            throttle.wait()
    return sent


def check_acceptances(page, db: DB, actions=live_actions, limit: int = 25) -> int:
    accepted = 0
    for p in db.by_status("invited", limit=limit):
        try:
            if actions.is_connected(page, p["profile_url"]):
                db.set_status(p["id"], "connected")
                accepted += 1
                _log(f"{p['full_name']} accepted")
        except NeedsLogin:
            raise
        except Exception as exc:
            _log(f"could not check {p['full_name']}: {exc}")
    return accepted


def _send_one(page, db: DB, throttle: Throttle, actions, prospect, kind: str, body: str,
              message_id: int | None = None) -> bool:
    try:
        actions.send_message(page, prospect["profile_url"], body)
    except NeedsLogin:
        raise
    except Exception as exc:
        _log(f"{kind} to {prospect['full_name']} failed: {exc}")
        if message_id:
            db.update_message(message_id, state="failed", error=str(exc))
        else:
            db.add_message(prospect["id"], "out", kind, body, "failed", error=str(exc))
        return False
    if message_id:
        db.update_message(message_id, state="sent", sent_at=now())
    else:
        db.add_message(prospect["id"], "out", kind, body, "sent", sent_at=now())
    db.touch_contacted(prospect["id"])
    throttle.record("message", prospect["id"])
    _log(f"sent {kind} to {prospect['full_name']}")
    return True


def send_intros(page, db: DB, settings: Settings, throttle: Throttle,
                actions=live_actions) -> int:
    sent = 0
    for p in db.by_status("connected"):
        ok, why = throttle.can("message")
        if not ok:
            _log(f"stopping intros: {why}")
            break
        if not _ok_to_contact(db, p["id"]):
            continue
        body = draft("intro", p, settings)
        if _send_one(page, db, throttle, actions, p, "intro", body):
            db.set_status(p["id"], "messaged")
            sent += 1
            throttle.wait()
    return sent


def _older_than(ts: str | None, days: int) -> bool:
    if not ts:
        return True
    return datetime.fromisoformat(ts) <= datetime.now(timezone.utc) - timedelta(days=days)


def send_followups(page, db: DB, settings: Settings, throttle: Throttle,
                   actions=live_actions) -> int:
    """One follow-up after N quiet days, then mark no_reply after another N days."""
    days = settings.followup_after_days
    for p in db.by_status("followed_up"):
        if _older_than(p["last_contacted_at"], days):
            db.set_status(p["id"], "no_reply")
    sent = 0
    for p in db.by_status("messaged"):
        if not _older_than(p["last_contacted_at"], days):
            continue
        ok, why = throttle.can("message")
        if not ok:
            _log(f"stopping follow-ups: {why}")
            break
        if not _ok_to_contact(db, p["id"]):
            continue
        body = draft("followup", p, settings)
        if _send_one(page, db, throttle, actions, p, "followup", body):
            db.set_status(p["id"], "followed_up")
            sent += 1
            throttle.wait()
    return sent


# --- inbound ------------------------------------------------------------------

def _is_from_prospect(sender: str, prospect) -> bool:
    """Message groups are labelled with the sender's display name; match it to the prospect."""
    s = " ".join((sender or "").lower().split())
    full = " ".join(prospect["full_name"].lower().split())
    return bool(s) and (s == full or full.startswith(s) or s.startswith(full))


def handle_inbound(db: DB, settings: Settings, prospect, text: str) -> str:
    """Store one inbound message, classify it, and queue the right next step."""
    db.add_message(prospect["id"], "in", "inbound", text, "received")
    intent = classify(text, settings)
    db.set_status(prospect["id"], STATUS_FOR_INTENT[intent])
    _log(f"reply from {prospect['full_name']}: {intent}")

    if intent == "opt_out":
        # Drop anything still queued for them.
        for m in db.messages_for(prospect["id"]):
            if m["direction"] == "out" and m["state"] in ("draft", "approved"):
                db.update_message(m["id"], state="discarded")
        return intent
    if intent == "bot_question":
        db.flag_for_human(prospect["id"], "Asked whether they're talking to a bot. Answer this one yourself.")
        return intent
    if intent == "not_interested":
        return intent

    history = [dict(m) for m in db.messages_for(prospect["id"])
               if m["state"] in ("sent", "received")]
    try:
        body, needs_human = draft_reply(db.get(prospect["id"]), history, settings)
    except Exception as exc:
        body, needs_human = "", True
        _log(f"reply draft failed: {exc}")
    if needs_human or not body:
        db.flag_for_human(prospect["id"], "Needs a reply from you (no draft could be made).")
        return intent
    state = "approved" if settings.reply_mode == "auto" else "draft"
    db.add_message(prospect["id"], "out", "reply", body, state)
    return intent


def poll_inbox(page, db: DB, settings: Settings, actions=live_actions,
               max_threads: int = 20) -> dict:
    counts: dict[str, int] = {}
    for m in actions.read_recent_threads(page, max_threads=max_threads):
        p = db.by_url(m.profile_url)
        if not p or not _is_from_prospect(m.sender, p):
            continue
        if db.has_inbound(p["id"], m.body):
            continue
        intent = handle_inbound(db, settings, p, m.body)
        counts[intent] = counts.get(intent, 0) + 1
    return counts


def send_approved(page, db: DB, throttle: Throttle, actions=live_actions) -> int:
    """Send replies you approved (or auto-approved replies in auto mode)."""
    sent = 0
    for m in db.messages_in_state("approved"):
        if m["prospect_status"] == "do_not_contact":
            db.update_message(m["id"], state="discarded")
            continue
        ok, why = throttle.can("message")
        if not ok:
            _log(f"stopping replies: {why}")
            break
        try:
            validate_outbound("reply", m["body"])
        except DraftRejected as exc:
            db.update_message(m["id"], state="failed", error=str(exc))
            continue
        p = db.get(m["prospect_id"])
        if _send_one(page, db, throttle, actions, p, m["kind"], m["body"], message_id=m["id"]):
            sent += 1
            throttle.wait()
    return sent


def daily_cycle(page, db: DB, settings: Settings, throttle: Throttle,
                actions=live_actions, discover_when_below: int = 40) -> dict:
    """What `haulerbot run` does: inbox first, then replies, then new outreach."""
    report = {"inbox": poll_inbox(page, db, settings, actions)}
    report["replies_sent"] = send_approved(page, db, throttle, actions)
    report["accepted"] = check_acceptances(page, db, actions)
    report["intros_sent"] = send_intros(page, db, settings, throttle, actions)
    report["followups_sent"] = send_followups(page, db, settings, throttle, actions)
    if len(db.by_status("new")) < discover_when_below:
        report["discovered"] = discover(page, db)
    report["invites_sent"] = send_invites(page, db, settings, throttle, actions)
    return report
