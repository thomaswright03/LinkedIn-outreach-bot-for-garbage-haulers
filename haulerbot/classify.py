"""Reply classification (build step 5).

Deterministic rules run first and always win for the two cases that must never be
missed: opt-outs (the prospect is marked do-not-contact) and "am I talking to a bot?"
questions (the conversation is handed to you). Claude only breaks ties on the rest.
"""

from __future__ import annotations

import json
import re

from .config import Settings

INTENTS = ("opt_out", "bot_question", "interested", "not_interested", "question", "other")

_OPT_OUT = re.compile(
    r"^\s*(stop|unsubscribe|remove me|no|nope|pass)\s*[.!]*\s*$"
    r"|\b(stop (messaging|contacting|texting) me|don'?t (message|contact) me|do not (message|contact)"
    r"|take me off|remove me|unsubscribe|leave me alone|not interested|no thanks|no thank you"
    r"|we'?re (all )?(set|good)|we are (all )?(set|good)|we'?ll pass)\b",
    re.I,
)

_BOT_QUESTION = re.compile(
    r"\b(are|is) (you|this|u)\s+(a |an )?(bot|ai|robot|automated|chat ?gpt|real( person)?|human|a person)\b"
    r"|\b(bot|ai|automated|chatgpt)\s*\?"
    r"|\b(did|does) (a |an )?(bot|ai|chatgpt|software|program) (write|send|wrote)"
    r"|\b(talking|speaking|chatting) (to|with) (a |an )?(bot|ai|robot|machine|real person|human)"
    r"|\bautomated (message|reply|response)s?\b",
    re.I,
)

_INTERESTED = re.compile(
    r"\b(interested|sounds good|tell me more|send (me )?(more )?info|let'?s (talk|chat|connect|set)"
    r"|set up a (call|time|meeting|demo)|what'?s (the )?(cost|price|pricing)|how much"
    r"|when are you free|give me a call|call me|my (number|cell|email) is|book a (call|demo)"
    r"|curious|open to it|happy to (chat|talk|take a look))\b",
    re.I,
)


def rule_intent(text: str) -> str | None:
    if _BOT_QUESTION.search(text):
        return "bot_question"
    if _OPT_OUT.search(text):
        # "not interested right now but..." still counts as an opt-out; we err toward
        # leaving people alone.
        return "opt_out"
    if _INTERESTED.search(text):
        return "interested"
    return None


_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string", "enum": list(INTENTS)}},
    "required": ["intent"],
    "additionalProperties": False,
}

_SYSTEM = """Classify a prospect's LinkedIn reply to a sales intro about a tool that follows up on abandoned dumpster-rental carts for garbage haulers.
- opt_out: they want no further contact, or clearly decline
- bot_question: they ask whether they're talking to a bot, AI, or automated system
- interested: they want to learn more, talk, see pricing, or book time
- not_interested: a soft no without asking to stop (e.g. "not right now", "we already have something")
- question: a question that needs an answer before they decide
- other: anything else (thanks for connecting, off-topic)"""


def classify(text: str, settings: Settings | None = None, use_claude: bool | None = None) -> str:
    rule = rule_intent(text)
    if rule:
        return rule
    from .messaging import claude_available, _client
    if use_claude is None:
        use_claude = claude_available()
    if not use_claude or settings is None:
        return "other"
    try:
        response = _client().beta.messages.create(
            model=settings.model,
            max_tokens=2000,
            system=_SYSTEM,
            messages=[{"role": "user", "content": text}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            return "other"
        body = "".join(b.text for b in response.content if b.type == "text")
        intent = json.loads(body)["intent"]
        return intent if intent in INTENTS else "other"
    except Exception as exc:
        print(f"  (classification fell back to 'other': {exc})")
        return "other"


# How each intent moves the prospect.
STATUS_FOR_INTENT = {
    "opt_out": "do_not_contact",
    "not_interested": "not_interested",
    "interested": "interested",
    "question": "replied",
    "other": "replied",
    "bot_question": "replied",
}
