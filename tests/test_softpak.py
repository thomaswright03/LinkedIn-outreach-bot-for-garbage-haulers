"""The Soft-Pak seed workbook flow: import, profile lookup, tiers, pitch, export."""

import csv
import sqlite3

import pytest
from openpyxl import Workbook

from haulerbot import campaign
from haulerbot.db import DB
from haulerbot.discovery import Candidate
from haulerbot.messaging import DraftRejected, _check_softpak, draft, render
from haulerbot.report import export_companies_csv
from haulerbot.seed import import_seed, is_verified_flag, normalize_priority, normalize_softpak

from test_campaign import FakeLinkedIn


def _workbook(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Prospects"
    ws.append(["Soft-Pak MTS prospect seed"])  # a title row above the headers
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


@pytest.fixture
def seeded(db, tmp_path):
    path = _workbook(tmp_path / "softpak_mts_prospect_seed.xlsx", [
        ["Company", "Website", "Headquarters", "Residential Service", "Soft-Pak Status",
         "Soft-Pak Evidence", "Evidence URL", "Owner/CEO", "Sales/Growth Executive",
         "LinkedIn", "Business Email", "Email Verified", "Priority"],
        ["Acadiana Waste Services", "acadianawaste.com", "Lafayette, LA", "Yes", "Confirmed",
         "Soft-Pak article", "https://soft-pak.com/x", "Jane Doe", "", 
         "https://www.linkedin.com/in/jane-doe-aws/?trk=x", "jane@acadianawaste.com", "Yes", "A"],
        ["Meridian Waste", "", "", "Yes", "Confirmed (portal)", "", "", "Bob Ray; Sam Lee",
         "TBD", "", "info@meridian.com", "guessed from pattern", "Tier B"],
        ["Bertolotti Disposal", "", "", "", "Historical", "", "", "", "", "", "", "", "C"],
        ["Smith Hauling", "", "", "", "", "", "", "Al Smith", "", "", "al@smith.com", "", ""],
        ["", "", "", "", "", "", "", "", "", "", "", "", ""],
    ])
    return import_seed(db, path)


def test_import_company_per_row(db, seeded):
    assert seeded.companies == 4 and seeded.contacts == 4
    assert seeded.contacts_with_linkedin == 1
    aws = db.companies(priorities=("A",))[0]
    assert aws["name"] == "Acadiana Waste Services" and aws["softpak_status"] == "confirmed"
    jane = db.contacts_of(aws["id"])[0]
    assert jane["profile_url"] == "https://www.linkedin.com/in/jane-doe-aws/"
    assert jane["status"] == "new" and jane["role"] == "Owner/CEO" and jane["priority"] == "A"
    assert jane["email_verified"] == 1


def test_guessed_emails_never_verified(db, seeded):
    for p in db.all_prospects():
        if p["company"] != "Acadiana Waste Services":
            assert p["email_verified"] == 0
    # Two people in one cell: the row's single email can't be attributed, so it's dropped.
    meridian = [p for p in db.all_prospects() if p["company"] == "Meridian Waste"]
    assert {p["full_name"] for p in meridian} == {"Bob Ray", "Sam Lee"}
    assert all(p["status"] == "needs_profile" and not p["email"] for p in meridian)


def test_reimport_is_idempotent(db, seeded, tmp_path):
    again = import_seed(db, tmp_path / "softpak_mts_prospect_seed.xlsx")
    assert again.contacts == 0 and len(db.companies()) == 4


def test_contact_per_row_csv(db, tmp_path):
    path = tmp_path / "contacts.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Company Name", "Contact Name", "Title", "LinkedIn URL", "Email",
                    "Email Status", "Priority", "Soft-Pak"])
        w.writerow(["Marin Sanitary Service", "Pat Kim", "VP Sales",
                    "linkedin.com/in/patkim", "pat@marinsanitary.com", "verified", "A", "Yes"])
        w.writerow(["Marin Sanitary Service", "Lou Fox", "COO", "", "lou@marinsanitary.com",
                    "pattern guess", "A", "Yes"])
    r = import_seed(db, path)
    assert (r.companies, r.contacts, r.unverified_emails) == (1, 2, 1)
    pat, lou = db.all_prospects()
    assert pat["email_verified"] == 1 and lou["email_verified"] == 0


@pytest.mark.parametrize("v,want", [("A", "A"), ("tier b", "B"), ("C - research", "C"),
                                    ("", ""), ("high", "")])
def test_priority(v, want):
    assert normalize_priority(v) == want


def test_softpak_and_verification_parsing():
    assert normalize_softpak("Confirmed (case study)") == "confirmed"
    assert normalize_softpak("Probable") == "probable"
    assert normalize_softpak("historical / former") == "historical"
    assert not is_verified_flag("Guessed")
    assert not is_verified_flag("")
    assert is_verified_flag("Verified")


def test_v1_database_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE prospects (id INTEGER PRIMARY KEY, profile_url TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL, first_name TEXT NOT NULL, headline TEXT DEFAULT '',
            company TEXT DEFAULT '', location TEXT DEFAULT '', source_query TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new', needs_human INTEGER NOT NULL DEFAULT 0,
            needs_human_reason TEXT DEFAULT '', notes TEXT DEFAULT '', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, last_contacted_at TEXT);
        INSERT INTO prospects (profile_url, full_name, first_name, status, created_at, updated_at)
        VALUES ('https://www.linkedin.com/in/x/', 'X Y', 'X', 'messaged', 't', 't');
    """)
    conn.commit()
    conn.close()
    db = DB(path)
    p = db.all_prospects()[0]
    assert p["status"] == "messaged" and p["company_id"] is None and p["priority"] == ""


def test_c_tier_is_research_only(db, settings, throttle, seeded):
    li = FakeLinkedIn()
    c = [c for c in db.companies() if c["priority"] == "C"][0]
    db.add_prospect("https://www.linkedin.com/in/bert/", "Bert Olo", "Bert", "Owner",
                    company_id=c["id"])
    campaign.send_invites(None, db, settings, throttle, li)
    invited = {url for url, _ in li.invites}
    assert "https://www.linkedin.com/in/jane-doe-aws/" in invited
    assert "https://www.linkedin.com/in/bert/" not in invited


def test_find_profiles_matches_name_and_company(db, settings, seeded):
    results = {
        "Bob Ray": [Candidate("https://www.linkedin.com/in/bob-ray-1/", "Bob Ray", "Bob",
                              "Owner at Some Plumbing", "", ""),
                    Candidate("https://www.linkedin.com/in/bob-ray-mw/", "Robert Ray", "Robert",
                              "President at Meridian Waste", "", ""),
                    Candidate("https://www.linkedin.com/in/bob-ray-2/", "Bob Ray", "Bob",
                              "CEO, Meridian Waste", "", "")],
        "Sam Lee": [],
    }

    def scrape(page, url):
        return next((v for k, v in results.items() if k.replace(" ", "+") in url), [])

    out = campaign.find_profiles(None, db, settings, scrape=scrape)
    assert out == {"found": 1, "not_found": 2}  # Sam Lee, and Al Smith from the untiered row
    bob = [p for p in db.all_prospects() if p["full_name"] == "Bob Ray"][0]
    assert bob["profile_url"] == "https://www.linkedin.com/in/bob-ray-2/" and bob["status"] == "new"
    # After a second miss, Sam is handed to a person instead of being searched forever.
    campaign.find_profiles(None, db, settings, scrape=scrape)
    sam = [p for p in db.all_prospects() if p["full_name"] == "Sam Lee"][0]
    assert sam["needs_human"] == 1
    assert campaign.find_profiles(None, db, settings, scrape=scrape) == {"found": 0, "not_found": 0}


def test_enrich_adds_decision_makers_at_the_company(db, settings, seeded):
    hits = [
        Candidate("https://www.linkedin.com/in/vp/", "Val Park", "Val", "VP of Sales at Acadiana Waste Services", "", ""),
        Candidate("https://www.linkedin.com/in/drv/", "Dee Rv", "Dee", "Driver at Acadiana Waste Services", "", ""),
        Candidate("https://www.linkedin.com/in/oth/", "Oz Th", "Oz", "Owner at Other Disposal", "", ""),
    ]
    out = campaign.enrich_companies(None, db, settings,
                                    scrape=lambda page, url: hits if "Acadiana" in url else [])
    assert out["contacts_added"] == 1
    aws = db.companies(priorities=("A",))[0]
    assert {p["full_name"] for p in db.contacts_of(aws["id"])} == {"Jane Doe", "Val Park"}
    # Only the A and B companies are searched; C (and untiered) companies aren't.
    assert out["companies_searched"] == 2


def test_search_cap(db, settings, throttle, seeded):
    settings.max_searches_per_day = 1
    calls = []
    campaign.find_profiles(None, db, settings, scrape=lambda p, u: calls.append(u) or [],
                           throttle=throttle)
    assert len(calls) == 1


def test_softpak_pitch_only_for_softpak_users(db, settings, seeded):
    aws = db.companies(priorities=("A",))[0]
    jane = db.contacts_of(aws["id"])[0]
    intro = render("intro", jane, settings, aws)
    assert "Soft-Pak" in intro and "Max" in intro and "instead of replacing" in intro
    smith = [c for c in db.companies() if c["name"] == "Smith Hauling"][0]
    al = db.contacts_of(smith["id"])[0]
    general = render("intro", al, settings, smith)
    assert "Soft-Pak" not in general and "Max" in general
    for kind in ("invite_note", "intro", "followup"):
        draft(kind, jane, settings, company=aws)  # all within limits and valid
    with pytest.raises(DraftRejected):
        _check_softpak("Since you run Soft-Pak...", smith)


def test_booking_link(db, settings, seeded):
    settings.booking_link = "https://cal.example.com/max"
    aws = db.companies(priorities=("A",))[0]
    assert "https://cal.example.com/max" in render("intro", db.contacts_of(aws["id"])[0], settings, aws)


def test_demo_booking_and_export(db, settings, seeded, tmp_path):
    aws = db.companies(priorities=("A",))[0]
    jane = db.contacts_of(aws["id"])[0]
    db.book_demo(jane["id"], "2026-10-08 2pm CT")
    db.set_status(jane["id"], "replied")  # a later reply doesn't undo the booking
    assert db.get(jane["id"])["status"] == "demo_booked"

    out = tmp_path / "companies.csv"
    assert export_companies_csv(db, out) == 4
    rows = {r["Company"]: r for r in csv.DictReader(out.open())}
    a = rows["Acadiana Waste Services"]
    assert a["Owner/CEO"] == "Jane Doe" and a["Demo Booked"] == "Yes"
    assert a["Outreach Status"] == "demo_booked"
    assert a["Business Email"] == "jane@acadianawaste.com (verified)"
    assert rows["Smith Hauling"]["Business Email"] == "al@smith.com (unverified)"


def test_enrich_attaches_profile_to_named_contact(db, settings, seeded):
    hits = [Candidate("https://www.linkedin.com/in/bobray/", "Bob Ray", "Bob",
                      "Owner at Meridian Waste", "", "")]
    campaign.enrich_companies(None, db, settings,
                              scrape=lambda page, url: hits if "Meridian" in url else [])
    bobs = [p for p in db.all_prospects() if p["full_name"] == "Bob Ray"]
    assert len(bobs) == 1 and bobs[0]["profile_url"] == "https://www.linkedin.com/in/bobray/"
