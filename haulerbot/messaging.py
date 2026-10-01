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

# Two template sets. "softpak" is for companies the seed workbook marks as confirmed or
# probable Soft-Pak users: MTS is pitched as the residential growth layer on top of
# Soft-Pak, never as a replacement. "general" is for everyone else (e.g. found by the
# generic LinkedIn search), so nobody is told they run Soft-Pak unless we have evidence.
# Both aim at the same goal: a short demo with {demo_with}.

GENERAL = {
    "invite_note": (
        "Hi {first_name}, I work with roll-off and residential haulers on turning website "
        "leads into booked jobs. Would be good to connect. - {sender_name}"),
    "intro": """Thanks for connecting, {first_name}.

Quick one: we built MTS, a residential growth tool for haulers. Part of it is what we run for Mountain High Disposal: it automatically follows up with people who start a dumpster rental or service signup on the website and then don't finish.{case_study_line}

Most haulers I talk to have a pile of those abandoned quotes just sitting there. That's stale data doing nothing, and every one of them is someone who already wanted service.

Would it be worth 20 minutes with {demo_first} to see if it'd work for {company_or_team}?{booking_line}

{sender_name}""",
    "followup": """Hi {first_name}, just bumping this in case it got buried. Happy to have {demo_first} walk you through how it works for Mountain High if abandoned online orders are something {company_or_team} deals with. If it's not a fit, no worries at all.

{sender_name}""",
}

SOFTPAK = {
    "invite_note": (
        "Hi {first_name}, I work with haulers on Soft-Pak on residential growth: online "
        "signups, abandoned quotes, door-to-door sales. Would be good to connect. - {sender_name}"),
    "intro": """Thanks for connecting, {first_name}.

Reaching out because {company_or_team} runs Soft-Pak, and we built MTS specifically around residential growth for haulers. It sits on top of Soft-Pak instead of replacing it: online customer signup, following up on abandoned quotes and carts, door-to-door and geofenced sales, and getting the new customer data back into your existing workflow.{case_study_line}

{demo_first} is showing a few Soft-Pak operators what we're doing. Would you be open to 20 minutes on the calendar?{booking_line}

{sender_name}""",
    "followup": """Hi {first_name}, bumping this in case it got buried. If growing residential accounts is on the list this year, {demo_first} can show you in 20 minutes how MTS works alongside Soft-Pak. If it's not a fit, no worries at all.

{sender_name}""",
}

# Kept for callers that only need the general wording.
TEMPLATES = GENERAL


def uses_softpak(company) -> bool:
    return bool(company) and company["softpak_status"] in ("confirmed", "probable")


def templates_for(company) -> dict[str, str]:
    return SOFTPAK if uses_softpak(company) else GENERAL


def fields_for(prospect, settings: Settings) -> dict[str, str]:
    company = (prospect["company"] or "").strip()
    case = settings.case_study_result.strip()
    link = settings.booking_link.strip()
    return {
        "first_name": prospect["first_name"] or "there",
        "company": company,
        "company_or_team": company or "your team",
        "sender_name": settings.sender_name,
        "demo_first": settings.demo_with.split()[0] if settings.demo_with.strip() else "our team",
        "case_study_line": f" {case.rstrip('.')}." if case else "",
        "booking_line": f" Here's a link to grab a time: {link}" if link else "",
    }


def render(kind: str, prospect, settings: Settings, company=None) -> str:
    template = templates_for(company)[kind]
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

VOICE_SYSTEM = """You write LinkedIn messages that {sender} sends from {sender}'s own account to owners and decision-makers at US waste-hauling companies (residential trash and recycling pickup, roll-off dumpster rental). {sender} reviews the campaign and answers conversations personally when needed.

What {sender} is offering: MTS (Max Tracking Solutions), a residential growth and customer-acquisition layer for haulers: online customer signup, automated follow-up on abandoned quotes and carts, door-to-door and geofenced sales territories, acquisition reporting, and getting new customer data back into the hauler's existing software. The abandoned-cart follow-up is what the team already runs for Mountain High Disposal. The angle: abandoned quotes are stale data the hauler is already sitting on, from people who already wanted service.

The goal of every conversation is a 20-minute demo with {demo_with}. Aim for a qualified demo, not volume: if the prospect clearly isn't a fit, let it go politely.

When the prospect's company is marked as a Soft-Pak user, position MTS as the growth layer that works alongside Soft-Pak, which stays their core operations and billing software. Never suggest replacing Soft-Pak. When the company is not marked as a Soft-Pak user, do not mention Soft-Pak at all.

Write in {sender}'s first-person voice: plain, friendly, direct, like a busy small-business owner texting another one. Short sentences. No buzzwords, no exclamation-mark enthusiasm, no emojis, no bullet points, no subject line, no sign-off beyond the first name.

Rules:
- Use only the facts given here and in the prospect details. Never invent results, numbers, client names, mutual connections, or personal details about the prospect. Don't quote internal research notes (account estimates, evidence sources) back to the prospect.
- Never state or imply that {sender} is typing this by hand, and never claim or deny anything about whether software helped write it. Just write the message.
- Do not use placeholders; everything must be filled in.
- Stay under the length limit you are given."""

REPLY_SYSTEM_EXTRA = """
You are drafting {sender}'s next reply in an existing conversation. Answer what the prospect actually said. If they are interested, suggest 20 minutes with {demo_with} and ask what days and times work{booking}. If they ask something you can't answer from the facts given (pricing, integration details for their setup, contract terms), say {demo_first} will cover it on the call or {sender} will get them the details. If they asked whether they're talking to a bot, an AI, or an automated system, do not answer that question: set needs_human to true and leave body empty."""


def _system(settings: Settings, reply: bool = False) -> str:
    f = fields_for({"first_name": "", "company": ""}, settings)
    text = VOICE_SYSTEM.format(sender=settings.sender_name, demo_with=settings.demo_with)
    if reply:
        booking = f", or offer this scheduling link: {settings.booking_link}" \
            if settings.booking_link else ""
        text += REPLY_SYSTEM_EXTRA.format(sender=settings.sender_name, demo_with=settings.demo_with,
                                          demo_first=f["demo_first"], booking=booking)
    return text


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


def _prospect_blurb(prospect, company=None) -> str:
    text = (f"Name: {prospect['full_name']}\nFirst name: {prospect['first_name']}\n"
            f"Headline: {prospect['headline']}\nRole: {prospect['role'] or 'unknown'}\n"
            f"Company: {prospect['company'] or 'unknown'}\n"
            f"Location: {prospect['location'] or 'unknown'}")
    if company is not None:
        text += (f"\nSoft-Pak user: {'yes' if uses_softpak(company) else 'not known'}"
                 f"\nResidential service: {company['residential_service'] or 'unknown'}"
                 f"\nMarkets: {company['markets'] or 'unknown'}")
    else:
        text += "\nSoft-Pak user: not known"
    return text


_SOFTPAK_WORD = re.compile(r"soft[\s-]?pak", re.I)


def _check_softpak(body: str, company) -> str:
    if not uses_softpak(company) and _SOFTPAK_WORD.search(body):
        raise DraftRejected("mentions Soft-Pak for a company not known to use it")
    return body


def draft(kind: str, prospect, settings: Settings, use_claude: bool | None = None,
          company=None) -> str:
    """Return a validated outbound message for `kind` (invite_note, intro, followup)."""
    baseline = render(kind, prospect, settings, company)
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
            f"Prospect:\n{_prospect_blurb(prospect, company)}\n{facts}\n\n"
            f"Here is the baseline template; keep its substance but make it read naturally "
            f"for this person:\n---\n{baseline}\n---\n"
            f"Set needs_human to false.")
    try:
        out = _ask_claude(settings, _system(settings), user)
        return _check_softpak(validate_outbound(kind, out["body"]), company)
    except Exception as exc:  # fall back to the template rather than skipping a prospect
        print(f"  (Claude draft unavailable for {prospect['full_name']}: {exc}; using template)")
        return validate_outbound(kind, baseline)


def draft_reply(prospect, history: list[dict], settings: Settings,
                company=None) -> tuple[str, bool]:
    """Draft the next reply. Returns (body, needs_human)."""
    if not claude_available():
        return "", True
    convo = "\n".join(f"{'Prospect' if m['direction'] == 'in' else settings.sender_name}: {m['body']}"
                      for m in history)
    facts = f"\nVerified result {settings.sender_name} may cite: {settings.case_study_result}" \
        if settings.case_study_result else ""
    user = (f"Prospect:\n{_prospect_blurb(prospect, company)}{facts}\n\nConversation so far:\n{convo}\n\n"
            f"Write {settings.sender_name}'s next message. Hard limit: {MESSAGE_LIMIT} characters.")
    out = _ask_claude(settings, _system(settings, reply=True), user)
    if out.get("needs_human"):
        return "", True
    return _check_softpak(validate_outbound("reply", out["body"]), company), False
