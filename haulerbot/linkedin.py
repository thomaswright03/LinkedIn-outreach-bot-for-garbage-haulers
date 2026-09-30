"""LinkedIn UI actions via Playwright (build steps 3 and 5).

Everything that touches LinkedIn's DOM lives here so that when their markup changes,
this is the only file to fix. Selectors prefer accessible roles and visible text over
class names, which LinkedIn obfuscates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .browser import NeedsLogin, human_pause, is_checkpoint, type_like_a_person
from .discovery import canonical_profile_url

MESSAGING_URL = "https://www.linkedin.com/messaging/"


class ActionFailed(RuntimeError):
    pass


def _open_profile(page, profile_url: str) -> None:
    page.goto(profile_url, wait_until="domcontentloaded")
    human_pause(2.5, 5)
    if is_checkpoint(page.url):
        raise NeedsLogin("LinkedIn asked for login/verification.")
    page.mouse.wheel(0, 400)
    human_pause(1, 2.5)
    page.mouse.wheel(0, -400)
    human_pause(0.5, 1.5)


def _top_card(page):
    return page.locator("main section").first


def connection_state(page) -> str:
    """'connected', 'pending', or 'not_connected' for the open profile."""
    card = _top_card(page)
    if card.get_by_role("button", name=re.compile(r"^Pending", re.I)).count():
        return "pending"
    if card.get_by_text(re.compile(r"·\s*1st\b")).count():
        return "connected"
    return "not_connected"


def send_connection_request(page, profile_url: str, note: str) -> str:
    """Returns 'sent', 'already_connected', or 'already_pending'."""
    _open_profile(page, profile_url)
    state = connection_state(page)
    if state == "connected":
        return "already_connected"
    if state == "pending":
        return "already_pending"

    card = _top_card(page)
    connect = card.get_by_role("button", name=re.compile(r"Invite .* to connect|^Connect$", re.I))
    if not connect.count():
        # Connect is often tucked under the "More" menu.
        more = card.get_by_role("button", name=re.compile(r"^More", re.I))
        if not more.count():
            raise ActionFailed("no Connect or More button on profile")
        more.first.click()
        human_pause()
        connect = page.get_by_role("button", name=re.compile(r"Invite .* to connect", re.I))
        if not connect.count():
            connect = page.get_by_role("menuitem", name=re.compile(r"Connect", re.I))
        if not connect.count():
            raise ActionFailed("Connect option not available (may require email or be restricted)")
    connect.first.click()
    human_pause()

    dialog = page.get_by_role("dialog")
    add_note = dialog.get_by_role("button", name=re.compile(r"Add a note", re.I))
    if note and add_note.count():
        add_note.first.click()
        human_pause()
        box = dialog.locator("textarea").first
        type_like_a_person(box, note)
        human_pause()
    send = dialog.get_by_role("button", name=re.compile(r"^Send", re.I))
    if not send.count():
        raise ActionFailed("Send button missing in invite dialog (weekly invite limit or email required?)")
    send.first.click()
    human_pause(2, 4)
    if page.get_by_text(re.compile(r"weekly invitation limit|reached the limit", re.I)).count():
        raise ActionFailed("LinkedIn weekly invitation limit reached")
    return "sent"


def is_connected(page, profile_url: str) -> bool:
    _open_profile(page, profile_url)
    return connection_state(page) == "connected"


def send_message(page, profile_url: str, body: str) -> None:
    _open_profile(page, profile_url)
    card = _top_card(page)
    btn = card.get_by_role("button", name=re.compile(r"^Message", re.I))
    if not btn.count():
        btn = card.get_by_role("link", name=re.compile(r"^Message", re.I))
    if not btn.count():
        raise ActionFailed("no Message button (not connected yet?)")
    btn.first.click()
    human_pause(2, 3.5)
    box = page.locator("div.msg-form__contenteditable[contenteditable='true']").last
    if not box.count():
        box = page.get_by_role("textbox", name=re.compile(r"Write a message", re.I)).last
    if not box.count():
        raise ActionFailed("message box did not open")
    # Type paragraph by paragraph; Shift+Enter keeps line breaks without sending.
    paras = body.split("\n")
    for i, line in enumerate(paras):
        if line:
            type_like_a_person(box, line)
        if i < len(paras) - 1:
            box.press("Shift+Enter")
    human_pause(1, 2.5)
    send = page.locator("button.msg-form__send-button").last
    if not send.count():
        send = page.get_by_role("button", name=re.compile(r"^Send$", re.I)).last
    send.click()
    human_pause(1.5, 3)
    _close_chat_bubbles(page)


def _close_chat_bubbles(page) -> None:
    for btn in page.get_by_role("button", name=re.compile(r"Close your (conversation|draft)", re.I)).all():
        try:
            btn.click()
            human_pause(0.3, 0.8)
        except Exception:
            pass


@dataclass
class InboxMessage:
    profile_url: str
    sender: str  # display name on the message group; compared to the prospect's name
    body: str


_READ_THREAD_JS = r"""
() => {
  const out = [];
  const profile = document.querySelector('.msg-thread a[href*="/in/"], .msg-entity-lockup a[href*="/in/"], a.msg-thread__link-to-profile');
  const url = profile ? profile.href : '';
  let lastSender = '';
  for (const li of document.querySelectorAll('li.msg-s-message-list__event')) {
    const nameEl = li.querySelector('.msg-s-message-group__name, .msg-s-message-group__profile-link');
    if (nameEl) lastSender = nameEl.innerText.trim();
    const bodyEl = li.querySelector('.msg-s-event-listitem__body');
    if (!bodyEl) continue;
    out.push({url, sender: lastSender, body: bodyEl.innerText.trim()});
  }
  return out;
}
"""


def read_recent_threads(page, max_threads: int = 20) -> list[InboxMessage]:
    """Open the most recent conversations and return every message in them."""
    page.goto(MESSAGING_URL, wait_until="domcontentloaded")
    human_pause(3, 5)
    if is_checkpoint(page.url):
        raise NeedsLogin("LinkedIn asked for login/verification.")
    items = page.locator("li.msg-conversation-listitem, li.msg-conversations-container__convo-item")
    out: list[InboxMessage] = []
    for i in range(min(items.count(), max_threads)):
        items.nth(i).click()
        human_pause(1.5, 3)
        for raw in page.evaluate(_READ_THREAD_JS):
            url = canonical_profile_url(raw.get("url", ""))
            if url and raw.get("body"):
                out.append(InboxMessage(url, raw.get("sender", ""), raw["body"]))
    return out
