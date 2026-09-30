"""Command-line interface: `haulerbot <command>`."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager

from . import campaign
from .browser import NeedsLogin, ensure_logged_in, interactive_login, open_context, page_of
from .config import Settings
from .db import DB, STATUSES
from .ratelimit import Throttle
from .report import dashboard, export_csv


@contextmanager
def _live(settings: Settings):
    with open_context(settings) as ctx:
        page = page_of(ctx)
        ensure_logged_in(page)
        yield page


def cmd_login(s, db, args):
    return 0 if interactive_login(s) else 1


def cmd_discover(s, db, args):
    queries = args.query or campaign.DEFAULT_QUERIES
    with _live(s) as page:
        print(json.dumps(campaign.discover(page, db, queries, args.pages), indent=2))


def cmd_invite(s, db, args):
    with _live(s) as page:
        n = campaign.send_invites(page, db, s, Throttle(db, s), limit=args.limit)
    print(f"sent {n} connection requests")


def cmd_message(s, db, args):
    t = Throttle(db, s)
    with _live(s) as page:
        accepted = campaign.check_acceptances(page, db)
        intros = campaign.send_intros(page, db, s, t)
        followups = campaign.send_followups(page, db, s, t)
    print(f"{accepted} newly accepted, {intros} intros, {followups} follow-ups sent")


def cmd_poll(s, db, args):
    with _live(s) as page:
        print(json.dumps(campaign.poll_inbox(page, db, s, max_threads=args.threads), indent=2))


def cmd_send_approved(s, db, args):
    with _live(s) as page:
        n = campaign.send_approved(page, db, Throttle(db, s))
    print(f"sent {n} approved replies")


def cmd_run(s, db, args):
    with _live(s) as page:
        report = campaign.daily_cycle(page, db, s, Throttle(db, s))
    print(json.dumps(report, indent=2))


def cmd_review(s, db, args):
    """Walk through drafted replies: approve, edit, discard, or skip each one."""
    drafts = db.messages_in_state("draft")
    if not drafts:
        print("No drafts waiting.")
    for m in drafts:
        print("\n" + "=" * 70)
        print(f"#{m['prospect_id']} {m['full_name']} ({m['company']})  {m['profile_url']}")
        for h in db.messages_for(m["prospect_id"]):
            if h["state"] in ("sent", "received"):
                who = "THEM" if h["direction"] == "in" else "YOU "
                print(f"  {who}: {h['body']}")
        print("-" * 70 + f"\nDRAFT:\n{m['body']}\n" + "-" * 70)
        choice = input("[a]pprove  [e]dit  [d]iscard  [s]kip  [q]uit > ").strip().lower()
        if choice == "a":
            db.update_message(m["id"], state="approved")
        elif choice == "e":
            print("Type the new message. End with a line containing only a single '.'")
            lines = []
            while (line := input()) != ".":
                lines.append(line)
            db.update_message(m["id"], body="\n".join(lines).strip(), state="approved")
        elif choice == "d":
            db.update_message(m["id"], state="discarded")
        elif choice == "q":
            break
    print("Approved replies go out on the next `haulerbot run` or `haulerbot send-approved`.")


def cmd_reply(s, db, args):
    """Queue your own reply to a prospect (e.g. one flagged for you) and clear the flag."""
    p = db.get(args.prospect_id)
    if not p:
        print("no such prospect")
        return 1
    if p["status"] == "do_not_contact":
        print(f"{p['full_name']} opted out; not queuing a message.")
        return 1
    db.add_message(p["id"], "out", "reply", args.text, "approved")
    db.clear_human_flag(p["id"])
    print("Queued. It goes out on the next run or `haulerbot send-approved`.")


def cmd_show(s, db, args):
    p = db.get(args.prospect_id)
    if not p:
        print("no such prospect")
        return 1
    for k in p.keys():
        print(f"{k:>20}: {p[k]}")
    print()
    for m in db.messages_for(p["id"]):
        print(f"[{m['direction']}/{m['kind']}/{m['state']}] {m['sent_at'] or m['created_at']}\n  {m['body']}")


def cmd_set_status(s, db, args):
    db.set_status(args.prospect_id, args.status, force=True)
    if args.status != "do_not_contact":
        db.clear_human_flag(args.prospect_id)
    print("ok")


def cmd_status(s, db, args):
    print(dashboard(db, Throttle(db, s)))


def cmd_export(s, db, args):
    n = export_csv(db, args.out)
    print(f"wrote {n} prospects to {args.out}")


def cmd_pause(s, db, args):
    db.set_kv("paused", "1")
    print("paused: no invites or messages will go out until `haulerbot resume`")


def cmd_resume(s, db, args):
    db.set_kv("paused", "0")
    print("resumed")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="haulerbot", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="open a browser and sign in to LinkedIn by hand").set_defaults(fn=cmd_login)

    d = sub.add_parser("discover", help="search LinkedIn for hauler decision-makers")
    d.add_argument("--query", action="append", help="search keywords (repeatable)")
    d.add_argument("--pages", type=int, default=2, help="result pages per query")
    d.set_defaults(fn=cmd_discover)

    i = sub.add_parser("invite", help="send connection requests to new prospects")
    i.add_argument("--limit", type=int, help="stop after this many (still capped per day)")
    i.set_defaults(fn=cmd_invite)

    sub.add_parser("message", help="check acceptances, send intros and follow-ups").set_defaults(fn=cmd_message)

    po = sub.add_parser("poll", help="read the inbox and classify replies")
    po.add_argument("--threads", type=int, default=20)
    po.set_defaults(fn=cmd_poll)

    sub.add_parser("review", help="approve, edit or discard drafted replies").set_defaults(fn=cmd_review)
    sub.add_parser("send-approved", help="send replies you approved").set_defaults(fn=cmd_send_approved)
    sub.add_parser("run", help="the full daily cycle").set_defaults(fn=cmd_run)

    r = sub.add_parser("reply", help="queue your own message to a prospect")
    r.add_argument("prospect_id", type=int)
    r.add_argument("text")
    r.set_defaults(fn=cmd_reply)

    sh = sub.add_parser("show", help="show a prospect and their conversation")
    sh.add_argument("prospect_id", type=int)
    sh.set_defaults(fn=cmd_show)

    st = sub.add_parser("set-status", help="manually set a prospect's status")
    st.add_argument("prospect_id", type=int)
    st.add_argument("status", choices=STATUSES)
    st.set_defaults(fn=cmd_set_status)

    sub.add_parser("status", help="campaign dashboard").set_defaults(fn=cmd_status)

    e = sub.add_parser("export", help="export prospects and outcomes to CSV")
    e.add_argument("--out", default="exports/prospects.csv")
    e.set_defaults(fn=cmd_export)

    sub.add_parser("pause", help="stop all outbound activity").set_defaults(fn=cmd_pause)
    sub.add_parser("resume", help="resume outbound activity").set_defaults(fn=cmd_resume)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    db = DB(settings.db_path)
    try:
        return args.fn(settings, db, args) or 0
    except NeedsLogin as exc:
        print(exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nstopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
