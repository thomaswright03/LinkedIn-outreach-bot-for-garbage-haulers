from datetime import datetime, timedelta, timezone

from haulerbot import campaign
from haulerbot.discovery import Candidate
from haulerbot.linkedin import InboxMessage
from haulerbot.report import export_csv


class FakeLinkedIn:
    def __init__(self):
        self.invites, self.messages, self.inbox, self.connected = [], [], [], set()

    def send_connection_request(self, page, url, note):
        self.invites.append((url, note))
        return "sent"

    def is_connected(self, page, url):
        return url in self.connected

    def send_message(self, page, url, body):
        self.messages.append((url, body))

    def read_recent_threads(self, page, max_threads=20):
        return list(self.inbox)


def test_discover_dedupes_and_filters(db):
    results = [
        Candidate("https://www.linkedin.com/in/a/", "Ann Lee", "Ann", "Owner at Ann's Dumpsters", "Ann's Dumpsters", ""),
        Candidate("https://www.linkedin.com/in/b/", "Bo Ray", "Bo", "Owner at Bo's Plumbing", "Bo's Plumbing", ""),
    ]
    out = campaign.discover(None, db, queries=["q1", "q2"], pages_per_query=1,
                            scrape=lambda page, url: results)
    assert out == {"found": 4, "added": 1, "not_a_fit": 2}
    assert [p["full_name"] for p in db.all_prospects()] == ["Ann Lee"]


def test_full_happy_path(db, settings, throttle, prospect):
    li = FakeLinkedIn()
    assert campaign.send_invites(None, db, settings, throttle, li) == 1
    assert db.get(prospect["id"])["status"] == "invited"

    li.connected.add(prospect["profile_url"])
    assert campaign.check_acceptances(None, db, li) == 1
    assert campaign.send_intros(None, db, settings, throttle, li) == 1
    assert db.get(prospect["id"])["status"] == "messaged"
    assert "Mountain High Disposal" in li.messages[0][1]

    li.inbox = [InboxMessage(prospect["profile_url"], "Jane Doe", "Sounds good, how much is it?")]
    assert campaign.poll_inbox(None, db, settings, li) == {"interested": 1}
    assert db.get(prospect["id"])["status"] == "interested"
    # No API key in tests, so no draft can be made: it's handed to Thomas.
    assert db.get(prospect["id"])["needs_human"] == 1

    # Polling again doesn't double-count the same reply.
    assert campaign.poll_inbox(None, db, settings, li) == {}


def test_opt_out_is_permanent(db, settings, throttle, prospect):
    li = FakeLinkedIn()
    db.set_status(prospect["id"], "messaged")
    db.add_message(prospect["id"], "out", "reply", "queued thing", "approved")
    li.inbox = [InboxMessage(prospect["profile_url"], "Jane Doe", "Please stop messaging me")]
    campaign.poll_inbox(None, db, settings, li)
    assert db.get(prospect["id"])["status"] == "do_not_contact"

    # Nothing automated can revive them or send them anything.
    db.set_status(prospect["id"], "connected")
    assert db.get(prospect["id"])["status"] == "do_not_contact"
    assert campaign.send_approved(None, db, throttle, li) == 0
    assert campaign.send_intros(None, db, settings, throttle, li) == 0
    assert li.messages == []


def test_bot_question_goes_to_human_with_no_draft(db, settings, throttle, prospect):
    li = FakeLinkedIn()
    db.set_status(prospect["id"], "messaged")
    li.inbox = [InboxMessage(prospect["profile_url"], "Jane Doe", "wait, is this a bot?")]
    campaign.poll_inbox(None, db, settings, li)
    p = db.get(prospect["id"])
    assert p["needs_human"] == 1 and "bot" in p["needs_human_reason"]
    assert not [m for m in db.messages_for(p["id"]) if m["direction"] == "out"]


def test_own_messages_in_thread_are_ignored(db, settings, prospect):
    li = FakeLinkedIn()
    li.inbox = [InboxMessage(prospect["profile_url"], "Thomas Wright", "Thanks for connecting")]
    assert campaign.poll_inbox(None, db, settings, li) == {}


def test_daily_cap_and_pause(db, settings, throttle):
    for i in range(5):
        db.add_prospect(f"https://www.linkedin.com/in/p{i}/", f"P{i} X", f"P{i}",
                        "Owner at X Disposal", "X Disposal")
    settings.max_invites_per_day = 3
    li = FakeLinkedIn()
    assert campaign.send_invites(None, db, settings, throttle, li) == 3
    assert campaign.send_invites(None, db, settings, throttle, li) == 0

    settings.max_invites_per_day = 10
    db.set_kv("paused", "1")
    assert campaign.send_invites(None, db, settings, throttle, li) == 0


def test_weekend_and_night_are_off(db, settings):
    from haulerbot.ratelimit import Throttle
    sat = Throttle(db, settings, clock=lambda: datetime(2026, 10, 3, 10, 0))
    night = Throttle(db, settings, clock=lambda: datetime(2026, 9, 29, 22, 0))
    assert not sat.can("invite")[0] and not night.can("message")[0]


def test_followup_then_no_reply(db, settings, throttle, prospect):
    li = FakeLinkedIn()
    old = (datetime.now(timezone.utc) - timedelta(days=settings.followup_after_days + 1)).isoformat()
    db.set_status(prospect["id"], "messaged")
    db.conn.execute("UPDATE prospects SET last_contacted_at = ? WHERE id = ?", (old, prospect["id"]))
    assert campaign.send_followups(None, db, settings, throttle, li) == 1
    assert db.get(prospect["id"])["status"] == "followed_up"
    db.conn.execute("UPDATE prospects SET last_contacted_at = ? WHERE id = ?", (old, prospect["id"]))
    campaign.send_followups(None, db, settings, throttle, li)
    assert db.get(prospect["id"])["status"] == "no_reply"
    assert len(li.messages) == 1  # only ever one follow-up


def test_auto_mode_queues_reply_for_sending(db, settings, throttle, prospect, monkeypatch):
    settings.reply_mode = "auto"
    monkeypatch.setattr(campaign, "draft_reply", lambda p, h, s: ("Great, does Tuesday work?", False))
    li = FakeLinkedIn()
    li.inbox = [InboxMessage(prospect["profile_url"], "Jane Doe", "Tell me more")]
    campaign.poll_inbox(None, db, settings, li)
    assert campaign.send_approved(None, db, throttle, li) == 1
    assert li.messages[-1][1] == "Great, does Tuesday work?"


def test_export(db, prospect, tmp_path):
    db.add_message(prospect["id"], "in", "inbound", "interested", "received")
    out = tmp_path / "x.csv"
    assert export_csv(db, out) == 1
    text = out.read_text()
    assert "Jane Doe" in text and "interested" in text
