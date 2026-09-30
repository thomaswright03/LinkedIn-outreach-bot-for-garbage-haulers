import pytest

from haulerbot.classify import classify, rule_intent


@pytest.mark.parametrize("text,intent", [
    ("STOP", "opt_out"),
    ("Not interested, thanks", "opt_out"),
    ("please remove me from your list", "opt_out"),
    ("No thanks, we're all set", "opt_out"),
    ("Are you a bot?", "bot_question"),
    ("is this automated", "bot_question"),
    ("Am I talking to a real person here?", "bot_question"),
    ("did chatgpt write this lol", "bot_question"),
    ("Sounds good, what's the pricing?", "interested"),
    ("Sure, give me a call Thursday", "interested"),
])
def test_rules(text, intent):
    assert rule_intent(text) == intent


def test_bot_question_wins_over_everything():
    assert rule_intent("Interested, but are you a bot?") == "bot_question"


def test_ambiguous_without_claude_is_other(settings):
    assert classify("Thanks for connecting!", settings) == "other"
