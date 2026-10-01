import pytest

from haulerbot.messaging import (INVITE_NOTE_LIMIT, DraftRejected, draft, render,
                                 validate_outbound)


def test_templates_render_fully(prospect, settings):
    for kind in ("invite_note", "intro", "followup"):
        body = draft(kind, prospect, settings)
        assert "{" not in body and "Jane" in body
    assert len(draft("invite_note", prospect, settings)) <= INVITE_NOTE_LIMIT
    assert "Mountain High Disposal" in render("intro", prospect, settings)
    assert "Peak Roll-Off" in render("intro", prospect, settings)


def test_case_study_only_when_provided(prospect, settings):
    assert "%" not in render("intro", prospect, settings)
    settings.case_study_result = "It recovered 14 orders in the first month"
    assert "It recovered 14 orders in the first month." in render("intro", prospect, settings)


def test_missing_company_reads_naturally(db, settings):
    p = db.get(db.add_prospect("https://www.linkedin.com/in/x/", "Al Ray", "Al", "Owner"))
    assert "your team" in render("intro", p, settings)


@pytest.mark.parametrize("bad", [
    "Hi {first_name}, quick question",
    "Hi [First Name], quick question",
    "I'm a real person, not a bot, promise.",
    "No bots here, just me.",
    "",
])
def test_validate_rejects(bad):
    with pytest.raises(DraftRejected):
        validate_outbound("intro", bad)


def test_invite_note_length_limit():
    with pytest.raises(DraftRejected):
        validate_outbound("invite_note", "x" * 201)
