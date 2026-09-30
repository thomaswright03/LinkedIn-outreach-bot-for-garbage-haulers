"""Browser session management (build step 1: authentication).

We never store your LinkedIn password. `haulerbot login` opens a real browser window
with a persistent profile; you sign in by hand (including 2FA or any captcha), and the
session cookies stay in data/browser-profile for later runs. If LinkedIn logs the
session out or shows a security checkpoint, commands stop and ask you to log in again.
"""

from __future__ import annotations

import random
import time
from contextlib import contextmanager
from typing import Iterator

from .config import Settings

FEED_URL = "https://www.linkedin.com/feed/"
LOGIN_URL = "https://www.linkedin.com/login"


class NeedsLogin(RuntimeError):
    """The saved session is gone or LinkedIn wants a human (checkpoint/captcha)."""


@contextmanager
def open_context(settings: Settings, headless: bool | None = None) -> Iterator:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(settings.browser_profile_dir),
            headless=settings.headless if headless is None else headless,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        try:
            yield ctx
        finally:
            ctx.close()


def page_of(ctx):
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def is_checkpoint(url: str) -> bool:
    return any(part in url for part in ("/checkpoint/", "/authwall", "/login", "/uas/login"))


def ensure_logged_in(page) -> None:
    page.goto(FEED_URL, wait_until="domcontentloaded")
    human_pause(2, 4)
    if is_checkpoint(page.url):
        raise NeedsLogin(
            "LinkedIn session is not logged in (or hit a security checkpoint). "
            "Run `haulerbot login` and sign in by hand.")


def interactive_login(settings: Settings, timeout_minutes: int = 10) -> bool:
    """Open a visible browser and wait for the user to finish signing in."""
    with open_context(settings, headless=False) as ctx:
        page = page_of(ctx)
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        print("A browser window is open. Sign in to LinkedIn (complete any 2FA), "
              "then leave the window on your feed.")
        deadline = time.time() + timeout_minutes * 60
        while time.time() < deadline:
            if "/feed" in page.url and not is_checkpoint(page.url):
                print("Logged in. Session saved to", settings.browser_profile_dir)
                return True
            time.sleep(2)
        print("Timed out waiting for login.")
        return False


def human_pause(low: float = 0.8, high: float = 2.5) -> None:
    """Short randomized pause between UI steps so actions aren't machine-regular."""
    time.sleep(random.uniform(low, high))


def type_like_a_person(locator, text: str) -> None:
    locator.click()
    for chunk in _chunks(text):
        locator.press_sequentially(chunk, delay=random.randint(25, 90))
        if random.random() < 0.08:
            time.sleep(random.uniform(0.3, 1.2))


def _chunks(text: str, size: int = 12):
    for i in range(0, len(text), size):
        yield text[i:i + size]
