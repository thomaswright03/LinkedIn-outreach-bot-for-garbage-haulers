# haulerbot

LinkedIn outreach for waste haulers. It works through a target list of haulers that run
**Soft-Pak** (from the seed workbook), finds the decision-makers there, sends them a
connection request and a short intro from your own LinkedIn account, and works each
conversation toward one goal: **a 20-minute MTS demo with Max Garrett**. It tracks
everything in a database laid out like the Soft-Pak prospecting plan.

The pitch: *Soft-Pak runs your operations; MTS is the residential growth layer on top of
it.* That means online signup, follow-up on abandoned quotes and carts (what we run for
Mountain High Disposal), door-to-door and geofenced sales, and new customer data flowing
back into the existing workflow. Companies that aren't known Soft-Pak users get a general
version of the pitch that never mentions Soft-Pak.

## The Soft-Pak seed workbook

```bash
haulerbot import softpak_mts_prospect_seed.xlsx   # or a .csv export of it
haulerbot companies                                # see what landed, by A/B/C tier
```

The importer matches columns loosely, so the plan's column names (Company, Website,
Headquarters, States/Markets, Residential Service, Estimated Residential Accounts, Soft-Pak
Status, Soft-Pak Evidence, Evidence URL, Owner/CEO, President, COO, GM/Regional President,
Sales/Growth Executive, Marketing Executive, IT/CIO, LinkedIn, Business Email, Business
Phone, MTS Fit, Priority, Next Action, Demo Booked, Demo Date) and close variants all work.
It accepts either one row per company with names in the role columns, or one row per
contact with Contact Name / Title / LinkedIn / Email columns. Re-importing an updated
workbook adds new companies and contacts and fills in blanks without creating duplicates.

- **Priority.** A-tier companies are worked first, then B. C is research-only and never
  contacted (change with `HAULERBOT_CONTACT_PRIORITIES`).
- **Emails.** An email is marked verified only when the sheet has a verification column
  that explicitly says so. Anything else, including addresses guessed from a company's
  email pattern, stays "unverified" everywhere, including the export. The bot only sends
  LinkedIn messages; emails are stored for your records.
- **Finding people.** Contacts named in the sheet without a LinkedIn URL are looked up by
  name and company. A profile is attached only when the name matches and the headline names
  the company (or there is exactly one name match). After two misses the contact is flagged
  for you (`haulerbot set-profile ID URL`). Companies with fewer than two reachable contacts
  get a search for decision-makers there (owner, president, GM, ops, sales, marketing, IT),
  up to four per company.
- **Generic search** for hauler owners anywhere in the US only runs when the seeded list
  runs dry (turn it off with `HAULERBOT_GENERIC_SEARCH=0`).
- **Demos.** When someone books, record it with `haulerbot demo ID "2026-10-08 2pm CT"`.
  It shows on the company in the export and stops automated follow-ups to them.

## How it works

1. **Log in once by hand.** `haulerbot login` opens a real browser; you sign in
   (including 2FA). The session is saved in `data/browser-profile`. Your password is never stored.
2. **Find prospects.** Seeded Soft-Pak companies first (see above). If those run out,
   a generic US people search for owners, presidents, GMs and ops leads at hauling
   companies. Profiles are deduplicated and only kept if the headline shows both a
   decision-maker title and a hauling signal.
3. **Invite.** Sends a connection request with a short note.
4. **Intro.** When someone accepts, sends the intro message.
5. **Follow up once.** If there's no reply after `HAULERBOT_FOLLOWUP_AFTER_DAYS`, sends
   one low-pressure bump. No reply after that marks them `no_reply` and they're left alone.
6. **Read replies.** Polls the inbox, sorts each reply (interested, not interested,
   opted out, question, other) and drafts your next reply, which steers interested
   prospects to a time with Max (or `HAULERBOT_BOOKING_LINK` if you set one).
7. **You approve.** `haulerbot review` walks through drafted replies. You can switch to
   auto-send with `HAULERBOT_REPLY_MODE=auto`.

Messages are written in your first-person voice from the templates in
`haulerbot/messaging.py`. With `ANTHROPIC_API_KEY` set, Claude (`claude-opus-5-5` by
default) rewrites each one for the specific prospect so they don't all read the same, and
drafts replies. Without a key, the templates are used as-is and every reply is handed to you.

### Guardrails

- **Opt-outs are permanent.** "Stop", "not interested", "remove me" and similar mark the
  prospect `do_not_contact`, discard anything queued for them, and nothing automated can
  message them again. Only `haulerbot set-status` by hand can undo it.
- **"Is this a bot?" goes to you.** If a prospect asks whether they're talking to a bot,
  an AI or an automated system, no reply is drafted. The conversation is flagged in
  `haulerbot status` for you to answer yourself. Drafts that claim to be human or deny
  automation are rejected before they can be sent.
- **No invented facts.** The drafter only cites results you put in
  `HAULERBOT_CASE_STUDY_RESULT`. Leave it blank and messages stay qualitative. It never
  tells a company it runs Soft-Pak unless the workbook marks it confirmed or probable, and
  drafts that do are rejected.

### Pacing and account risk

LinkedIn's User Agreement prohibits automated activity, and it restricts or bans accounts
it catches, so this runs on your real account at your own risk. To keep that risk low the defaults
are deliberately slow: 20 connection requests and 25 messages per rolling 24 hours, a
random 45 to 180 second gap between actions (with the occasional longer break), weekdays
8am to 6pm only, and human-speed typing. Raising the caps raises the risk. If LinkedIn
shows a security checkpoint, the run stops and asks you to log in again rather than
trying to get around it.

## Setup

Python 3.10+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env    # then fill it in
haulerbot login
haulerbot import softpak_mts_prospect_seed.xlsx
```

## Daily use

```bash
haulerbot run            # inbox, approved replies, acceptances, intros, follow-ups, new invites
haulerbot review         # approve / edit / discard drafted replies
haulerbot status         # dashboard: counts, today's usage, conversations that need you
haulerbot export         # exports/companies.csv (master database) and exports/prospects.csv
```

Run `haulerbot run` once or twice a day (by hand, or from cron / Task Scheduler during
working hours). Individual steps are also available:

| Command | What it does |
| --- | --- |
| `import FILE` | Import or update the Soft-Pak seed workbook (.xlsx or .csv) |
| `companies [--tier A]` | Seeded companies with tier, Soft-Pak status, contacts, demos |
| `find-profiles` | Look up LinkedIn profiles for seeded contacts and companies |
| `set-profile ID URL` | Attach a LinkedIn profile to a contact by hand |
| `demo ID "DATE"` | Record a booked demo with Max |
| `discover [--query "..."] [--pages N]` | Generic search for hauler owners |
| `invite [--limit N]` | Send connection requests to new prospects |
| `message` | Check acceptances, send intros and due follow-ups |
| `poll` | Read the inbox and sort replies |
| `send-approved` | Send replies you approved |
| `reply ID "text"` | Queue your own message to a prospect and clear their flag |
| `show ID` | A prospect's details and full conversation |
| `set-status ID STATUS` | Change a status by hand |
| `pause` / `resume` | Stop / restart all outbound activity |

### Statuses

`needs_profile` (seeded, LinkedIn not found yet) → `new` → `invited` → `connected` →
`messaged` → `followed_up` → `no_reply`. On a reply: `interested`, `not_interested`,
`replied` (question or other), or `do_not_contact`. The goal state is `demo_booked`.

## Configuration

All settings are environment variables; see `.env.example`. The main ones:

| Variable | Default | |
| --- | --- | --- |
| `HAULERBOT_MAX_INVITES_PER_DAY` | 20 | Connection requests per rolling 24h |
| `HAULERBOT_MAX_MESSAGES_PER_DAY` | 25 | Intros, follow-ups and replies per rolling 24h |
| `HAULERBOT_MIN_DELAY` / `MAX_DELAY` | 45 / 180 | Seconds between actions |
| `HAULERBOT_ACTIVE_HOURS` | 8-18 | Local hours, weekdays only |
| `HAULERBOT_FOLLOWUP_AFTER_DAYS` | 5 | Quiet days before the single follow-up |
| `HAULERBOT_REPLY_MODE` | draft | `draft` (you approve) or `auto` |
| `HAULERBOT_CASE_STUDY_RESULT` | blank | A real result from Mountain High the drafter may cite |
| `HAULERBOT_MODEL` | claude-opus-5-5 | Claude model for drafting and classification |
| `HAULERBOT_DEMO_WITH` | Max Garrett | Who prospects book the demo with |
| `HAULERBOT_BOOKING_LINK` | blank | Optional scheduling link offered to interested prospects |
| `HAULERBOT_CONTACT_PRIORITIES` | A,B | Seed tiers that get contacted |
| `HAULERBOT_MAX_SEARCHES_PER_DAY` | 30 | LinkedIn searches per rolling 24h |
| `HAULERBOT_GENERIC_SEARCH` | 1 | Fall back to the generic search when the seed list runs dry |

## Layout

```
haulerbot/
  browser.py     login, persistent session, human-paced typing
  seed.py        Soft-Pak seed workbook import
  discovery.py   search URLs, result parsing, dedupe, name/company matching
  linkedin.py    every LinkedIn page interaction (the only file with selectors)
  messaging.py   templates, Claude drafting, outbound guardrails
  classify.py    reply sorting (rules first, Claude for the ambiguous rest)
  ratelimit.py   daily caps, active hours, randomized gaps
  campaign.py    the steps that tie it together
  db.py          SQLite storage: companies, contacts, messages (data/haulerbot.sqlite3)
  report.py      CSV exports (master database + contacts) and dashboard
  cli.py         the `haulerbot` command
```

LinkedIn changes its page markup regularly. When a step starts failing with "button
missing" style errors, `linkedin.py` (and the small extractor script in `discovery.py`)
is where to update selectors.

## Tests

```bash
pytest
```

Tests cover discovery parsing, the seed workbook import, profile matching, A/B/C tiers,
the Soft-Pak and general pitches, templates and guardrails, reply classification, opt-out
handling, rate limits, demo tracking, the exports, and the full campaign flow against a
fake LinkedIn. They don't
touch LinkedIn or the Claude API.
