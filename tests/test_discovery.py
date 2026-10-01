from haulerbot.discovery import (canonical_profile_url, company_from_headline, first_name_of,
                                 qualifies, search_url, to_candidate)


def test_canonical_url_strips_tracking_and_dedupes():
    a = canonical_profile_url("https://www.linkedin.com/in/Jane-Doe-123/?miniProfileUrn=abc")
    b = canonical_profile_url("https://linkedin.com/in/jane-doe-123")
    assert a == b == "https://www.linkedin.com/in/jane-doe-123/"


def test_anonymized_members_are_skipped():
    assert canonical_profile_url("https://www.linkedin.com/in/ACoAAB123xyz") is None
    assert to_candidate({"url": "https://www.linkedin.com/in/x/", "name": "LinkedIn Member"}) is None


def test_first_name():
    assert first_name_of("JOHN SMITH") == "John"
    assert first_name_of("Robert (Bob) Jones, MBA") == "Robert"
    assert first_name_of("Dr. Amy Lee") == "Amy"
    assert first_name_of("DeShawn Carter") == "DeShawn"


def test_company_from_headline():
    assert company_from_headline("Owner at Peak Roll-Off | Dumpster Rentals") == "Peak Roll-Off"
    assert company_from_headline("Founder @ Big Bin Co") == "Big Bin Co"
    assert company_from_headline("Waste industry veteran") == ""


def test_qualifies_needs_title_and_hauler_signal():
    assert qualifies("Owner at Peak Roll-Off Dumpsters")
    assert qualifies("President", "Smith Sanitation")
    assert not qualifies("Owner at Smith Plumbing")          # not a hauler
    assert not qualifies("Driver at Peak Roll-Off Dumpsters")  # not a decision-maker
    assert not qualifies("Towing company owner")               # "ow" inside words shouldn't match


def test_search_url_is_us_people_search():
    url = search_url("roll-off dumpster owner", page=2)
    assert url.startswith("https://www.linkedin.com/search/results/people/?")
    assert "103644278" in url and "page=2" in url
