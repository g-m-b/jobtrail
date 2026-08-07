# Ingest format & writing your own provider

## The record

A provider returns a list of dicts — one per **application** (a company + role pair),
not one per email. `data/classified.sample.jsonl` is a working example.

```jsonc
{
  "company": "Northwind Systems",        // required
  "role": "Senior Backend Engineer",     // required — with company, forms the unique key
  "source": "ats",                       // ats | linkedin | agency | referral | direct | jobboard
  "stage": "offer",                      // furthest rung reached (see ladder below)
  "status": "offer",                     // active | rejected | offer | withdrawn
  "interview_at": "2026-04-17T09:00:00", // optional ISO-8601; drives /api/upcoming
  "contacts": [{"name": "Dana Reyes", "email": "dana.reyes@example.com"}],
  "events": [
    {"date": "2026-04-06", "stage": "applied", "sender": "careers@example.com",
     "subject": "Application received", "from_me": false}
  ]
}
```

Set `"is_application": false` on a record to have it skipped.

## The stage ladder

```
applied → acknowledged → recruiter_screen → assessment → interview → final → offer
```

`stage` is the **furthest rung reached**, never the current state. A rejected
application keeps the stage it got to — otherwise the funnel would be meaningless.

`status` is the outcome, and is separate from stage because rejection is not a rung.

**`ghosted` is not a status you can set.** It is derived at read time from
`rules.ghost_days` of silence, so the clock keeps running between syncs and changing
the threshold takes effect immediately.

## What the engine derives for you

| Field | How |
|---|---|
| `applied_on` | earliest event date |
| `stage` | max rung across events (your declared `stage` wins if it is further) |
| `initiator` | `outbound` if the first event is `applied`/`acknowledged`, else `inbound` |
| `last_activity` | latest event date |
| `ghosted` | `status == active` and silent for longer than `ghost_days` |

`initiator` matters: response rate is computed over **outbound only**. Counting a
recruiter's cold email as a response to an application you never sent produces
nonsense like a 100% response rate.

## Writing a provider

```python
# jobtrail/providers.py  (or your own module, imported before startup)
from jobtrail.providers import provider

@provider("myboard")
def from_myboard(cfg) -> list[dict]:
    api_key = cfg["ingest"]["myboard"]["api_key"]
    return [ ... ]   # records in the shape above
```

Then in `config.toml`:

```toml
[ingest]
provider = "myboard"

[ingest.myboard]
api_key = "..."
```

Rules a provider must honour:

- **Be idempotent.** Return the full current picture every time. The engine hashes each
  record and skips writes when nothing changed, so a 6-hourly sync of unchanged data
  costs zero writes and zero cache churn.
- **Raise on failure, don't return `[]`.** An empty list means "this user has no
  applications" and would look like data loss. An exception is caught, logged to
  `sync_runs`, and leaves both the database and the cache untouched.
- **Don't fight the user.** Rows edited in the UI have `manual = 1`; the engine keeps
  their stage/status/notes and still appends your new events to the timeline.
