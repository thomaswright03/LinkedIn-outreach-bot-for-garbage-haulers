"""Target discovery (build step 2): search queries, result parsing, qualification.

The pure functions here (URL building, profile normalization, qualification) are unit
tested. The live scraping step (`scrape_search_page`) runs a small script in the page to
pull result cards; LinkedIn changes its markup often, so selectors live in one place.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

# LinkedIn geo URN for the United States.
US_GEO_URN = "103644278"

# Decision-maker titles we want to reach.
TITLE_KEYWORDS = (
    "owner", "co-owner", "founder", "co-founder", "president", "ceo", "chief executive",
    "coo", "chief operating", "general manager", "gm", "vp of operations",
    "vice president of operations", "director of operations", "operations manager",
    "principal", "partner",
)

# Signals that the company is a hauler (roll-off or residential).
HAULER_KEYWORDS = (
    "dumpster", "roll-off", "roll off", "rolloff", "waste", "disposal", "hauling",
    "hauler", "junk removal", "trash", "garbage", "sanitation", "recycling",
    "residential pickup", "curbside", "refuse",
)

# Default searches: each is (keywords, title filter) run against US people search.
DEFAULT_QUERIES = (
    "roll-off dumpster owner",
    "dumpster rental owner",
    "residential trash service owner",
    "waste hauling company president",
    "disposal company owner",
    "roll off container CEO",
)


@dataclass
class Candidate:
    profile_url: str
    full_name: str
    first_name: str
    headline: str
    company: str
    location: str


def search_url(keywords: str, page: int = 1, geo_urn: str = US_GEO_URN) -> str:
    params = {
        "keywords": keywords,
        "origin": "FACETED_SEARCH",
        "geoUrn": f'["{geo_urn}"]',
    }
    if page > 1:
        params["page"] = str(page)
    return "https://www.linkedin.com/search/results/people/?" + urllib.parse.urlencode(params)


_PROFILE_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([^/?#]+)", re.I)


def canonical_profile_url(url: str) -> str | None:
    """Strip tracking params so the same person always dedupes to one URL."""
    m = _PROFILE_RE.search(url or "")
    if not m:
        return None
    slug = urllib.parse.unquote(m.group(1)).strip().lower()
    if not slug or slug.startswith("acoa"):  # anonymized "LinkedIn Member" results
        return None
    return f"https://www.linkedin.com/in/{slug}/"


_CREDENTIALS = re.compile(r",?\s+(?:MBA|CPA|PE|PhD|Jr\.?|Sr\.?|III|II)\b.*$", re.I)


def first_name_of(full_name: str) -> str:
    name = re.sub(r"[\(\[].*?[\)\]]", "", full_name)       # drop (nicknames) / [pronouns]
    name = _CREDENTIALS.sub("", name).strip()
    parts = [p for p in re.split(r"\s+", name) if p]
    if not parts:
        return ""
    first = parts[0].strip(".,")
    if first.lower() in ("mr", "mrs", "ms", "dr") and len(parts) > 1:
        first = parts[1].strip(".,")
    return first.capitalize() if first.isupper() or first.islower() else first


def company_from_headline(headline: str) -> str:
    """'Owner at Acme Roll-Off' -> 'Acme Roll-Off'. Best effort; blank if unclear."""
    m = re.search(r"(?:\bat\b|@)\s+(.+?)(?:\s*[|•·,]|$)", headline or "", re.I)
    return m.group(1).strip() if m else ""


def _has_any(text: str, words) -> bool:
    t = f" {text.lower()} "
    return any(re.search(rf"(?<![a-z]){re.escape(w)}s?(?![a-z])", t) for w in words)


def qualifies(headline: str, company: str = "") -> bool:
    """Decision-maker title AND hauling-company signal."""
    text = f"{headline} {company}"
    return _has_any(text, TITLE_KEYWORDS) and _has_any(text, HAULER_KEYWORDS)


def to_candidate(raw: dict) -> Candidate | None:
    url = canonical_profile_url(raw.get("url", ""))
    name = (raw.get("name") or "").strip()
    if not url or not name or name.lower() == "linkedin member":
        return None
    headline = (raw.get("headline") or "").strip()
    return Candidate(
        profile_url=url,
        full_name=name,
        first_name=first_name_of(name),
        headline=headline,
        company=company_from_headline(headline),
        location=(raw.get("location") or "").strip(),
    )


# Runs in the page. Pulls one dict per result card. Kept deliberately loose: it keys off
# profile links rather than class names, which LinkedIn obfuscates and rotates.
_EXTRACT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const cards = document.querySelectorAll('li, div[data-view-name="search-entity-result-universal-template"]');
  for (const card of cards) {
    const link = card.querySelector('a[href*="/in/"]');
    if (!link) continue;
    const href = link.href.split('?')[0];
    if (seen.has(href)) continue;
    const nameEl = link.querySelector('span[aria-hidden="true"]') || link;
    const name = (nameEl.innerText || '').split('\n')[0].trim();
    const lines = (card.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    const idx = lines.findIndex(l => l.startsWith(name));
    const rest = lines.slice(idx + 1).filter(l => !/^(•|View|Connect|Message|Follow|\d+(st|nd|rd|th))/.test(l) && l !== name);
    if (!name) continue;
    seen.add(href);
    out.push({url: href, name, headline: rest[0] || '', location: rest[1] || ''});
  }
  return out;
}
"""


# --- seeded companies (Soft-Pak workbook) -----------------------------------

_NAME_NOISE = re.compile(r"[\(\[].*?[\)\]]|,.*$|[^a-z\s'-]", re.I)
_COMPANY_STOPWORDS = {"inc", "llc", "co", "corp", "company", "the", "and", "of", "services",
                      "service", "group", "ltd"}


def _name_tokens(name: str) -> list[str]:
    return [t for t in _NAME_NOISE.sub(" ", name.lower()).split() if len(t) > 1 or t.isalpha()]


def same_person(candidate_name: str, full_name: str) -> bool:
    """First and last name both match (ignoring middle names, credentials, nicknames)."""
    a, b = _name_tokens(candidate_name), _name_tokens(full_name)
    if len(a) < 2 or len(b) < 2:
        return bool(a) and a == b
    return a[0] == b[0] and a[-1] == b[-1]


_INDUSTRY_WORDS = {"waste", "disposal", "sanitation", "sanitary", "recycling", "environmental",
                   "hauling", "refuse", "resource", "resources", "recovery", "solutions",
                   "trash", "garbage", "rolloff", "roll", "off", "dumpster", "dumpsters"}


def company_words(company: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", company.lower().replace("&", " "))
            if w not in _COMPANY_STOPWORDS and len(w) > 1}


def mentions_company(text: str, company: str) -> bool:
    """True if the headline names the company (all of its distinctive words appear)."""
    if "/" in company:  # "Texas Pride Disposal / TXP Environmental": either name counts
        return any(mentions_company(text, part) for part in company.split("/") if part.strip())
    want = company_words(company)
    if not want:
        return False
    have = set(re.findall(r"[a-z0-9]+", text.lower().replace("&", " ")))
    # "Pena's Disposal" must match on "pena", not on the industry word "disposal".
    distinctive = want - _INDUSTRY_WORDS
    return (distinctive or want) <= have


def is_decision_maker(headline: str) -> bool:
    return _has_any(headline, TITLE_KEYWORDS + (
        "vp", "vice president", "director", "sales", "growth", "marketing", "cio",
        "chief information", "it director", "regional president", "division president"))


def scrape_search_page(page, url: str) -> list[Candidate]:
    from .browser import human_pause, is_checkpoint, NeedsLogin

    page.goto(url, wait_until="domcontentloaded")
    human_pause(3, 6)
    if is_checkpoint(page.url):
        raise NeedsLogin("LinkedIn asked for login/verification during search.")
    # Scroll like a reader so lazy-loaded cards render.
    for _ in range(4):
        page.mouse.wheel(0, 700)
        human_pause(0.6, 1.6)
    raw = page.evaluate(_EXTRACT_JS)
    return [c for c in (to_candidate(r) for r in raw) if c]
