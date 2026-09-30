"""Message templates and drafting (build steps 3, 4 and 7).

Templates are the baseline and always work offline. When ANTHROPIC_API_KEY is set, the
drafter asks Claude to rewrite each message for the specific prospect so no two read
identically. Every draft goes through `validate_outbound` before it can be sent.
"""

from __future__ import annotations

import json
import os
import re
import string

from .config import Settings

# LinkedIn caps connection notes at 200 characters on free accounts (300 on premium).
INVITE_NOTE_LIMIT = 200
MESSAGE_LIMIT = 1200

INVITE_NOTE = (
    "Hi {first_name}, I work with roll-off and residential haulers on turning website "
    "leads into booked jobs. Would be good to connect. - {sender_name}"
)

INTRO = """Thanks for connecting, {first_name}.

Quick one: we built a tool for Mountain High Disposal that automatically follows up with people who start a dumpster rental on their website and then don't finish checking out.{case_study_line}

Most haulers I talk to have a pile of those abandoned quotes just sitting there. That's stale data doing nothing, and every one of them is someone who already wanted a dumpster.

Would it be worth 15 minutes to see if it'd work for {company_or_team}?

{sender_name}"""

FOLLOWUP = """Hi {first_name}, just bumping this in case it got buried. Happy to walk you through how it works for Mountain High if abandoned online orders are something {company_or_team} deals with. If it's not a fit, no worries at all.

{sender_name}"""

TEMPLATES = {"invite_note": INVITE_NOTE, "intro": INTRO, "followup": FOLLOWUP}


def fields_for(prospect, settings: Settings) -> dict[str, str]:
    company = (prospect["company"] or "").strip()
    case = settings.case_study_result.strip()
    return {
        "first_name": prospect["first_name"] or "there",
        "company": company,
        "company_or_team": company or "your team",
        "sender_name": settings.sender_name,
        "case_study_line": f" {case.rstrip('.')}." if case else "",
    }


def render(kind: str, prospect, settings: Settings) -> str:
    template = TEMPLATES[kind]
    needed = {f for _, f, _, _ in string.Formatter().parse(template) if f}
    values = fields_for(prospect, settings)
    missing = needed - values.keys()
    if missing:
        raise KeyError(f"template {kind} needs {missing}")
    return template.format(**values).strip()


# --- guardrails -----------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{\{?\s*[a-z_]+\s*\}?\}|\[(?:first name|company|name)\]", re.I)

# The system never generates a message that claims the sender is a human typing by hand
# or denies automation. If a prospect asks, the conversation goes to you instead.
_HUMANITY_CLAIMS = re.compile(
    r"\b(i'?m|i am|this is)\s+(not\s+(a\s+)?(bot|ai|robot|automated)|a\s+real\s+(person|human))"
    r"|\bnot\s+an?\s+(ai|bot)\b|\bno\s+bots?\s+here\b|\b100%\s+human\b",
    re.I,
)


class DraftRejected(ValueError):
    pass


def validate_outbound(kind: str, body: str) -> str:
    body = body.strip()
    if not body:
        raise DraftRejected("empty message")
    if _PLACEHOLDER.search(body):
        raise DraftRejected("unfilled placeholder in message")
    if _HUMANITY_CLAIMS.search(body):
        raise DraftRejected("message claims to be human / denies automation")
    limit = INVITE_NOTE_LIMIT if kind == "invite_note" else MESSAGE_LIMIT
    if len(body) > limit:
        raise DraftRejected(f"{kind} is {len(body)} chars; limit is {limit}")
    return body


# --- Claude drafting ------------------------------------------------------

VOICE_SYSTEM = """You write LinkedIn messages that {sender} sends from his own account to owners and operators of US garbage-hauling companies (roll-off dumpster rental and residential trash pickup). {sender} reviews the campaign and answers conversations himself when needed.

What {sender} is offering: the same tool his team built for Mountain High Disposal. When someone starts a dumpster rental or service signup on a hauler's website and abandons it, the tool follows up with them automatically and turns a share of those abandoned carts into booked jobs. The angle: those abandoned quotes are stale data the hauler is already sitting on, from people who already wanted a dumpster.

Write in {sender}'s first-person voice: plain, friendly, direct, like a busy small-business owner texting another one. Short sentences. No buzzwords, no exclamation-mark enthusiasm, no emojis, no bullet points, no subject line, no sign-off beyond his first name.

Rules:
- Use only the facts given here and in the prospect details. Never invent results, numbers, client names, mutual connections, or personal details about the prospect.
- Never state or imply that {sender} is typing this by hand, and never claim or deny anything about whether software helped write it. Just write the message.
- Do not use placeholders; everything must be filled in.
- Stay under the length limit you are given."""

REPLY_SYSTEM_EXTRA = """
You are drafting {sender}'s next reply in an existing conversation. Answer what the prospect actually said. If they are interested, suggest a short call and ask what time works. If they ask something you can't answer from the facts given (pricing, integrations with their specific website platform, contract terms), say {sender} will get them the details and ask the best way to reach them. If they asked whether they're talking to a bot, an AI, or an automated system, do not answer that question: set needs_human to true and leave body empty."""


def _client():
    import anthropic
    return anthropic.Anthropic()


def claude_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "body": {"type": "string"},
        "needs_human": {"type": "boolean"},
    },
    "required": ["body", "needs_human"],
    "additionalProperties": False,
}


def _ask_claude(settings: Settings, system: str, user: str) -> dict:
    client = _client()
    response = client.beta.messages.create(
        model=settings.model,
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": user}],
        thinking={"type": "adaptive"},
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": _DRAFT_SCHEMA}},
        # Server-side fallback: if the primary model declines, the API retries on a
        # fallback model inside the same call.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise DraftRejected("model declined to draft this message")
    text = "".join(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def _prospect_blurb(prospect) -> str:
    return (f"Name: {prospect['full_name']}\nFirst name: {prospect['first_name']}\n"
            f"Headline: {prospect['headline']}\nCompany: {prospect['company'] or 'unknown'}\n"
            f"Location: {prospect['location'] or 'unknown'}")


def draft(kind: str, prospect, settings: Settings, use_claude: bool | None = None) -> str:
    """Return a validated outbound message for `kind` (invite_note, intro, followup)."""
    baseline = render(kind, prospect, settings)
    if use_claude is None:
        use_claude = claude_available()
    if not use_claude:
        return validate_outbound(kind, baseline)

    limit = INVITE_NOTE_LIMIT if kind == "invite_note" else MESSAGE_LIMIT
    purpose = {
        "invite_note": "a connection request note (no pitch details yet, just a reason to connect)",
        "intro": "the first message after they accepted the connection request",
        "followup": "a single, low-pressure follow-up because they haven't replied to the intro",
    }[kind]
    facts = f"\nVerified result {settings.sender_name} may cite: {settings.case_study_result}" \
        if settings.case_study_result else "\nNo numeric results are available; keep it qualitative."
    user = (f"Write {purpose}. Hard limit: {limit} characters.\n\n"
            f"Prospect:\n{_prospect_blurb(prospect)}\n{facts}\n\n"
            f"Here is the baseline template; keep its substance but make it read naturally "
            f"for this person:\n---\n{baseline}\n---\n"
            f"Set needs_human to false.")
    system = VOICE_SYSTEM.format(sender=settings.sender_name)
    try:
        out = _ask_claude(settings, system, user)
        return validate_outbound(kind, out["body"])
    except Exception as exc:  # fall back to the template rather than skipping a prospect
        print(f"  (Claude draft unavailable for {prospect['full_name']}: {exc}; using template)")
        return validate_outbound(kind, baseline)


def draft_reply(prospect, history: list[dict], settings: Settings) -> tuple[str, bool]:
    """Draft the next reply. Returns (body, needs_human)."""
    if not claude_available():
        return "", True
    convo = "\n".join(f"{'Prospect' if m['direction'] == 'in' else settings.sender_name}: {m['body']}"
                      for m in history)
    facts = f"\nVerified result {settings.sender_name} may cite: {settings.case_study_result}" \
        if settings.case_study_result else ""
    user = (f"Prospect:\n{_prospect_blurb(prospect)}{facts}\n\nConversation so far:\n{convo}\n\n"
            f"Write {settings.sender_name}'s next message. Hard limit: {MESSAGE_LIMIT} characters.")
    system = VOICE_SYSTEM.format(sender=settings.sender_name) + \
        REPLY_SYSTEM_EXTRA.format(sender=settings.sender_name)
    out = _ask_claude(settings, system, user)
    if out.get("needs_human"):
        return "", True
    return validate_outbound("reply", out["body"]), False
