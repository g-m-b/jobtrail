# jobtrail

Turn a mailbox into a job-application pipeline: what you applied to, who replied, how far
each one got, who the recruiter was, and which sources are actually worth your time.

Job hunting generates hundreds of near-identical emails. A recruiter blast and a real
application confirmation look the same in a search. jobtrail classifies them once, keeps a
timeline per application, and answers the questions a mailbox can't: *what's my real
response rate, where did I stall, and what's scheduled next.*

- **FastAPI + SQLite backend**, React + Recharts frontend, no cloud services.
- **Scheduled sync** on a configurable interval, with a pluggable ingest provider.
- **Honest metrics** — outbound and inbound are never mixed, ghosting is derived from
  silence rather than claimed, and every number you can hand-correct.

## Quick start

```sh
git clone <your-fork> && cd jobtrail
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.toml config.toml

cp data/classified.sample.jsonl data/classified.jsonl   # or use a real provider
python3 -m jobtrail.cli sync

uvicorn jobtrail.api:app --port 8000     # terminal 1
cd web && npm install && npm run dev     # terminal 2 -> http://localhost:5173
```

## Where the data comes from

Ingest is a **provider**: a function returning application records. Pick one in
`config.toml`; write your own in ~20 lines (see `docs/ingest-format.md`).

| Provider | Credentials | Self-syncing | Notes |
|---|---|---|---|
| `jsonl` | none | via the file | An LLM agent (or you) writes `data/classified.jsonl`. Best classification quality — an agent reads context a regex can't. |
| `gmail` | Google OAuth | yes | Talks to the Gmail API directly. Rules-based classification, no LLM key, runs unattended. |

### Using the Gmail provider

```sh
pip install google-api-python-client google-auth-oauthlib
# Google Cloud Console -> enable Gmail API -> Credentials -> OAuth client ID -> Desktop app
# download as credentials.json into the repo root
python3 -m jobtrail.authorize          # one-time consent, scope is read-only
```

Then set `provider = "gmail"` in `config.toml`. It never sends, deletes, or modifies mail.

## The scheduler

```toml
[sync]
enabled        = true
interval_hours = 6      # fractions work: 0.5 = 30 min. Floor is 60s.
run_on_startup = true
jitter_seconds = 30
```

Any value is overridable by environment variable — `JOBTRAIL_SYNC_INTERVAL_HOURS=1`.

It runs inside the API process (asyncio, no extra dependency, no cron to install).
`GET /api/sync` shows recent runs; `POST /api/sync` forces one immediately.

Ship it as a service with `systemd`, `launchd`, or a container — one process gives you both
the API and the scheduler.

## Cache design

**Reads never touch SQLite.** The store holds applications in memory; stats, contacts and
filters are computed from that.

The write path is deliberate:

```
provider → diff (content hash) → SQLite commit → upsert ONLY changed keys into cache
```

The cache is **never cleared**. A sync that changes 2 of 400 applications re-reads exactly
those 2 rows and leaves 398 hot. If a sync finds nothing new it performs zero writes and
zero cache churn. If the provider throws, both the database and the cache are left
untouched — a broken feed shows stale data, never an empty dashboard.

> Honest scoping: at ~100 rows SQLite alone is already fast enough, and you will not feel a
> difference. This design earns its keep in the thousands. It is a single-process dict — run
> one uvicorn worker, or move to Redis before scaling out.

## Metrics that don't lie

Three decisions worth knowing about, because naive versions of each produce nonsense:

**Response rate counts outbound only.** A recruiter cold-emailing you is not a response to
an application you never sent. Blend the two and the number inflates badly — agency-sourced
conversations approach a 100% "response rate" purely because the recruiter always writes
first. Split apart, the outbound figure is typically less than half the blended one.

**Time-to-reply skips auto-acknowledgements.** An ATS confirmation lands the same second you
submit; counting it makes the median 0 days. Measured to the first human reply instead.

**Ghosting is measured from their last message, not the last activity.** Chasing a recruiter
is not a sign of life — using last activity meant a round of follow-ups made every dead
application look alive again. It is also derived at read time, so the clock keeps running
between syncs and changing `ghost_days` takes effect immediately.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/applications` | list; filters `status`, `company`, `q` |
| `GET /api/applications/{id}` | one application plus its full event timeline |
| `POST/PATCH/DELETE /api/applications` | manual corrections; sets `manual = 1` |
| `GET /api/stats` | funnel, response rate, per-source, per-month, medians |
| `GET /api/contacts` | recruiters, derived from applications |
| `GET /api/upcoming` | scheduled interviews still in the future |
| `GET /api/sync` · `POST /api/sync` | scheduler state · force a run |

## Tests

```sh
python3 tests/test_jobtrail.py     # 17 assertions, no framework
```

They cover the parts that would silently corrupt the picture: cache/DB write ordering,
sync idempotency, failure isolation, manual-edit precedence, ghosting semantics, and the
job-board noise filter.

## Known limits

1. **Email cannot see rounds held over phone, WhatsApp, or in person.** Recruiter-driven
   processes undercount. Every application is editable — click a row, fix the stage, put the
   real round history in Notes. Manual edits survive every subsequent sync; new emails still
   land in the timeline.
2. **The `gmail` provider is rules-based** and deliberately conservative: it drops what it
   cannot place, so it misses recruiters mailing from personal addresses. The `jsonl`
   provider with an LLM agent catches those.
3. **Company/role de-duplication makes mistakes.** Two threads about one job can become two
   rows. Fix one, delete the other.
4. **Stage is the furthest rung reached, not the current state.** A rejected application
   keeps the stage it got to, or the funnel would be meaningless.

## Accessibility & colour

The table is keyboard-operable (Tab, Enter/Space), the drawer is a real dialog with Escape
and focus restoration, and there's a skip link, visible focus rings and
`prefers-reduced-motion` support.

Chart colours come from a validated palette rather than taste: outcome slots are ordered so
green and red are never adjacent in a stack (worst adjacent colour-vision-deficiency
separation ΔE 9.1 light / 14.4 dark). Light and dark are separately stepped. Legends, direct
labels and a table view mean identity is never carried by colour alone.

## Licence

MIT — see `LICENSE`.
