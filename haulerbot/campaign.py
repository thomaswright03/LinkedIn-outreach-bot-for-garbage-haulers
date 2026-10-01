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
from .discovery import (DEFAULT_QUERIES, is_decision_maker, mentions_company, qualifies,
                        same_person, scrape_search_page, search_url)
from .messaging import DraftRejected, draft, draft_reply, validate_outbound
from .ratelimit import Throttle


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# --- discovery ----------------------------------------------------------------

def _search(page, url: str, scrape, throttle: Throttle | None):
    """One LinkedIn search, counted against the daily search cap when a throttle is given."""
    if throttle is not None:
        ok, why = throttle.can("search")
        if not ok:
            raise _SearchCapReached(why)
        throttle.record("search", None)
    return scrape(page, url)


class _SearchCapReached(Exception):
    pass


def discover(page, db: DB, queries=DEFAULT_QUERIES, pages_per_query: int = 2,
             scrape=scrape_search_page, throttle: Throttle | None = None) -> dict:
    """Generic search: hauler decision-makers anywhere in the US (no seed list needed)."""
    found = added = skipped = 0
    for q in queries:
        for n in range(1, pages_per_query + 1):
            try:
                results = _search(page, search_url(q, n), scrape, throttle)
            except _SearchCapReached as exc:
                _log(f"stopping search: {exc}")
                return {"found": found, "added": added, "not_a_fit": skipped}
            for c in results:
                found += 1
                if not qualifies(c.headline, c.company):
                    skipped += 1
                    continue
                if db.add_prospect(c.profile_url, c.full_name, c.first_name, c.headline,
                                   c.company, c.location, source_query=q):
                    added += 1
                    _log(f"new prospect: {c.full_name} | {c.headline}")
    return {"found": found, "added": added, "not_a_fit": skipped}


def find_profiles(page, db: DB, settings: Settings, scrape=scrape_search_page,
                  throttle: Throttle | None = None, limit: int = 20) -> dict:
    """Find LinkedIn profiles for seeded contacts we only know by name and company."""
    found = missed = 0
    todo = [p for p in db.by_status("needs_profile") if _tier_ok(p, settings)]
    for p in todo[:limit]:
        tries_key = f"profile_search:{p['id']}"
        tries = int(db.get_kv(tries_key, "0") or 0)
        if tries >= 2:
            continue
        try:
            results = _search(page, search_url(f"{p['full_name']} {p['company']}"), scrape, throttle)
        except _SearchCapReached as exc:
            _log(f"stopping profile search: {exc}")
            break
        db.set_kv(tries_key, str(tries + 1))
        matches = [c for c in results if same_person(c.full_name, p["full_name"])]
        at_company = [c for c in matches if mentions_company(c.headline, p["company"])]
        pick = at_company[0] if at_company else (matches[0] if len(matches) == 1 else None)
        if pick and db.set_profile(p["id"], pick.profile_url, pick.headline):
            found += 1
            _log(f"found {p['full_name']} ({p['company']}): {pick.profile_url}")
        else:
            missed += 1
            if tries + 1 >= 2:
                db.flag_for_human(p["id"], "Couldn't find this contact on LinkedIn. Add the "
                                  "profile with `haulerbot set-profile` or skip them.")
    return {"found": found, "not_found": missed}


def enrich_companies(page, db: DB, settings: Settings, scrape=scrape_search_page,
                     throttle: Throttle | None = None, per_company: int = 4,
                     limit: int = 10) -> dict:
    """For seeded companies with too few reachable contacts, search for decision-makers there."""
    searched = added = 0
    for c in db.companies(priorities=settings.contact_priorities):
        if searched >= limit:
            break
        contacts = db.contacts_of(c["id"])
        if sum(1 for x in contacts if x["profile_url"]) >= 2:
            continue
        key = f"company_search:{c['id']}"
        if db.get_kv(key):
            continue
        name = c["name"].split("/")[0].strip()
        try:
            results = _search(page, search_url(name), scrape, throttle)
        except _SearchCapReached as exc:
            _log(f"stopping company search: {exc}")
            break
        db.set_kv(key, now())
        searched += 1
        room = per_company - len(contacts)
        for cand in results:
            if room <= 0:
                break
            if not (mentions_company(cand.headline, c["name"]) and is_decision_maker(cand.headline)):
                continue
            # Someone already named in the sheet: attach the profile instead of adding a duplicate.
            known = next((x for x in contacts if not x["profile_url"]
                          and same_person(cand.full_name, x["full_name"])), None)
            if known:
                if db.set_profile(known["id"], cand.profile_url, cand.headline):
                    _log(f"found {known['full_name']} ({c['name']}): {cand.profile_url}")
                continue
            if db.add_prospect(cand.profile_url, cand.full_name, cand.first_name, cand.headline,
                               location=cand.location, source_query=f"company:{name}",
                               company_id=c["id"]):
                added += 1
                room -= 1
                _log(f"new contact at {c['name']}: {cand.full_name} | {cand.headline}")
    return {"companies_searched": searched, "contacts_added": added}


# --- outbound -----------------------------------------------------------------

def _tier_ok(p, settings: Settings) -> bool:
    """Seeded prospects are contacted only in the configured tiers (A and B by default).
    Prospects with no tier (from the generic search) are always eligible."""
    return not p["priority"] or p["priority"] in settings.contact_priorities


def _ok_to_contact(db: DB, prospect_id: int, settings: Settings) -> bool:
    p = db.get(prospect_id)
    return (bool(p) and p["status"] not in TERMINAL and not p["needs_human"]
            and _tier_ok(p, settings))


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
        if not _ok_to_contact(db, p["id"], settings):
            continue
        try:
            note = draft("invite_note", p, settings, company=db.get_company(p["company_id"]))
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
        if not _ok_to_contact(db, p["id"], settings):
            continue
        body = draft("intro", p, settings, company=db.get_company(p["company_id"]))
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
        if not _ok_to_contact(db, p["id"], settings):
            continue
        body = draft("followup", p, settings, company=db.get_company(p["company_id"]))
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
        body, needs_human = draft_reply(db.get(prospect["id"]), history, settings,
                                        company=db.get_company(prospect["company_id"]))
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


def _ready_pool(db: DB, settings: Settings) -> int:
    return sum(1 for p in db.by_status("new") if _tier_ok(p, settings))


def daily_cycle(page, db: DB, settings: Settings, throttle: Throttle,
                actions=live_actions, discover_when_below: int = 40,
                scrape=scrape_search_page) -> dict:
    """What `haulerbot run` does: inbox first, then replies, then new outreach.

    New prospects come from the seed workbook first (A tier, then B), and only fall back
    to the generic LinkedIn search when the seeded list runs dry."""
    report = {"inbox": poll_inbox(page, db, settings, actions)}
    report["replies_sent"] = send_approved(page, db, throttle, actions)
    report["accepted"] = check_acceptances(page, db, actions)
    report["intros_sent"] = send_intros(page, db, settings, throttle, actions)
    report["followups_sent"] = send_followups(page, db, settings, throttle, actions)
    if _ready_pool(db, settings) < discover_when_below:
        report["profiles"] = find_profiles(page, db, settings, scrape, throttle)
        report["enriched"] = enrich_companies(page, db, settings, scrape, throttle)
    if settings.generic_search and _ready_pool(db, settings) < discover_when_below:
        report["discovered"] = discover(page, db, scrape=scrape, throttle=throttle)
    report["invites_sent"] = send_invites(page, db, settings, throttle, actions)
    return report
