# haulerbot

LinkedIn outreach for garbage haulers. It finds owners and operators of US roll-off and
residential hauling companies, sends them a connection request and a short intro from
your own LinkedIn account, pitches the abandoned-cart follow-up tool we built for
Mountain High Disposal, and keeps track of who's interested.

The pitch in one line: *those abandoned dumpster quotes on your website are stale data
you're already sitting on, from people who already wanted a dumpster.*

## How it works

1. **Log in once by hand.** `haulerbot login` opens a real browser; you sign in
   (including 2FA). The session is saved in `data/browser-profile`. Your password is never stored.
2. **Discover.** Searches LinkedIn people search (US only) for owners, presidents, GMs and
   ops leads at dumpster, roll-off, disposal and residential trash companies. Profiles are
   deduplicated and only kept if the headline shows both a decision-maker title and a
   hauling signal.
3. **Invite.** Sends a connection request with a short note.
4. **Intro.** When someone accepts, sends the intro message.
5. **Follow up once.** If there's no reply after `HAULERBOT_FOLLOWUP_AFTER_DAYS`, sends
   one low-pressure bump. No reply after that marks them `no_reply` and they're left alone.
6. **Read replies.** Polls the inbox, sorts each reply (interested, not interested,
   opted out, question, other) and drafts your next reply.
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
  `HAULERBOT_CASE_STUDY_RESULT`. Leave it blank and messages stay qualitative.

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
```

## Daily use

```bash
haulerbot run            # inbox, approved replies, acceptances, intros, follow-ups, new invites
haulerbot review         # approve / edit / discard drafted replies
haulerbot status         # dashboard: counts, today's usage, conversations that need you
haulerbot export         # CSV of every prospect and outcome -> exports/prospects.csv
```

Run `haulerbot run` once or twice a day (by hand, or from cron / Task Scheduler during
working hours). Individual steps are also available:

| Command | What it does |
| --- | --- |
| `discover [--query "..."] [--pages N]` | Search and add new prospects |
| `invite [--limit N]` | Send connection requests to new prospects |
| `message` | Check acceptances, send intros and due follow-ups |
| `poll` | Read the inbox and sort replies |
| `send-approved` | Send replies you approved |
| `reply ID "text"` | Queue your own message to a prospect and clear their flag |
| `show ID` | A prospect's details and full conversation |
| `set-status ID STATUS` | Change a status by hand |
| `pause` / `resume` | Stop / restart all outbound activity |

### Statuses

`new` → `invited` → `connected` → `messaged` → `followed_up` → `no_reply`, and on a reply:
`interested`, `not_interested`, `replied` (question or other), or `do_not_contact`.

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

## Layout

```
haulerbot/
  browser.py     login, persistent session, human-paced typing
  discovery.py   search URLs, result parsing, dedupe, qualification
  linkedin.py    every LinkedIn page interaction (the only file with selectors)
  messaging.py   templates, Claude drafting, outbound guardrails
  classify.py    reply sorting (rules first, Claude for the ambiguous rest)
  ratelimit.py   daily caps, active hours, randomized gaps
  campaign.py    the steps that tie it together
  db.py          SQLite storage (data/haulerbot.sqlite3)
  report.py      CSV export and dashboard
  cli.py         the `haulerbot` command
```

LinkedIn changes its page markup regularly. When a step starts failing with "button
missing" style errors, `linkedin.py` (and the small extractor script in `discovery.py`)
is where to update selectors.

## Tests

```bash
pytest
```

Tests cover discovery parsing, templates and guardrails, reply classification, opt-out
handling, rate limits, and the full campaign flow against a fake LinkedIn. They don't
touch LinkedIn or the Claude API.
