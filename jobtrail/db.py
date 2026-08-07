"""Schema, stage model, and writes. The only module that touches SQLite."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

# Ordered ladder: "furthest reached" is a max() over this.
STAGES = [
    "applied",
    "acknowledged",
    "recruiter_screen",
    "assessment",
    "interview",
    "final",
    "offer",
]
STAGE_RANK = {s: i for i, s in enumerate(STAGES, start=1)}

# Terminal outcomes. 'ghosted' is deliberately absent — it is never stored, it is
# derived at read time from silence (see store.decorate).
STATUSES = ["active", "rejected", "offer", "withdrawn"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id           INTEGER PRIMARY KEY,
    company      TEXT NOT NULL,
    role         TEXT NOT NULL,
    source       TEXT NOT NULL,
    applied_on   TEXT,
    stage        TEXT NOT NULL,
    status       TEXT NOT NULL,
    initiator    TEXT NOT NULL DEFAULT 'outbound',
    notes        TEXT DEFAULT '',
    contacts     TEXT DEFAULT '[]',
    interview_at TEXT,
    manual       INTEGER DEFAULT 0,
    content_hash TEXT,
    updated_at   TEXT,
    UNIQUE (company, role)
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    msg_id      TEXT UNIQUE NOT NULL,
    date        TEXT NOT NULL,
    sender      TEXT NOT NULL,
    sender_name TEXT DEFAULT '',
    subject     TEXT NOT NULL,
    stage       TEXT NOT NULL,
    is_from_me  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_app ON events(app_id);
CREATE TABLE IF NOT EXISTS sync_runs (
    id        INTEGER PRIMARY KEY,
    started   TEXT NOT NULL,
    finished  TEXT,
    provider  TEXT,
    added     INTEGER DEFAULT 0,
    updated   INTEGER DEFAULT 0,
    events    INTEGER DEFAULT 0,
    ok        INTEGER DEFAULT 1,
    error     TEXT
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")  # scheduler writes while API reads
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    con.commit()


def msg_id(company: str, role: str, ev: dict) -> str:
    key = f"{company}|{role}|{ev['date']}|{ev['sender']}|{ev['subject']}"
    return hashlib.sha1(key.encode()).hexdigest()[:20]


def content_hash(app: dict) -> str:
    """Fingerprint of everything a provider controls. Unchanged hash => skip the write.
    This is what makes a 6-hourly sync cheap and what drives the cache upsert set."""
    payload = json.dumps(
        {
            "source": app.get("source"),
            "stage": app.get("stage"),
            "status": app.get("status"),
            "interview_at": app.get("interview_at"),
            "contacts": app.get("contacts", []),
            "events": sorted(
                (e["date"], e["sender"], e["subject"], e["stage"])
                for e in app.get("events", [])
            ),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:20]


def initiator_of(events: list[dict]) -> str:
    """Who opened the conversation. An application whose earliest event is already a
    recruiter screen was not applied to — a recruiter reached out first."""
    if not events:
        return "outbound"
    first = min(events, key=lambda e: e["date"])
    return "outbound" if first["stage"] in ("applied", "acknowledged") else "inbound"


def derive(app: dict, events: list[dict]) -> tuple[str | None, str, str]:
    """(applied_on, stage, status). Stage is the furthest rung any event reached.

    Ghosting is NOT decided here — it depends on today's date, so storing it would
    freeze the clock at the last sync.
    """
    dates = sorted(e["date"] for e in events)
    applied_on = dates[0] if dates else None
    stage = max(
        (e["stage"] for e in events), key=lambda s: STAGE_RANK.get(s, 0), default="applied"
    )
    if STAGE_RANK.get(app.get("stage", ""), 0) > STAGE_RANK.get(stage, 0):
        stage = app["stage"]
    status = app.get("status", "active")
    if status not in STATUSES:
        status = "active"
    return applied_on, stage, status


def is_ghosted(row: dict, ghost_days: int, today: date | None = None) -> bool:
    """Silence, not a stated outcome. Only 'active' applications can go quiet.

    Measured from the last inbound message, NOT the last activity: chasing a recruiter
    is not a sign of life. Using last_activity meant a round of follow-ups made every
    dead application look active again.
    """
    if row.get("status") != "active":
        return False
    last = row.get("last_inbound") or row.get("applied_on")
    if not last:
        return False
    today = today or date.today()
    return today - datetime.strptime(last, "%Y-%m-%d").date() > timedelta(days=ghost_days)


def upsert_application(con: sqlite3.Connection, app: dict) -> tuple[int, str]:
    """Write one application + its events. Returns (app_id, 'added'|'updated'|'unchanged').

    Never clobbers a row the user edited by hand (manual = 1) — but still records new
    events against it, so the timeline stays complete even when the stage is pinned.
    """
    events = app.get("events", [])
    applied_on, stage, status = derive(app, events)
    initiator = initiator_of(events)
    digest = content_hash(app)
    now = datetime.now().isoformat(timespec="seconds")

    row = con.execute(
        "SELECT id, manual, content_hash FROM applications WHERE company = ? AND role = ?",
        (app["company"], app["role"]),
    ).fetchone()

    if row is None:
        cur = con.execute(
            "INSERT INTO applications (company, role, source, applied_on, stage, status,"
            " initiator, contacts, interview_at, content_hash, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                app["company"],
                app["role"],
                app.get("source", "direct"),
                applied_on,
                stage,
                status,
                initiator,
                json.dumps(app.get("contacts", [])),
                app.get("interview_at"),
                digest,
                now,
            ),
        )
        app_id, outcome = cur.lastrowid, "added"
    else:
        app_id = row["id"]
        if row["content_hash"] == digest:
            outcome = "unchanged"
        elif row["manual"]:
            # Respect the human edit, but keep provider-owned facts fresh.
            con.execute(
                "UPDATE applications SET contacts=?, interview_at=?, content_hash=?,"
                " updated_at=? WHERE id=?",
                (json.dumps(app.get("contacts", [])), app.get("interview_at"), digest, now, app_id),
            )
            outcome = "updated"
        else:
            con.execute(
                "UPDATE applications SET source=?, applied_on=?, stage=?, status=?,"
                " initiator=?, contacts=?, interview_at=?, content_hash=?, updated_at=?"
                " WHERE id=?",
                (
                    app.get("source", "direct"),
                    applied_on,
                    stage,
                    status,
                    initiator,
                    json.dumps(app.get("contacts", [])),
                    app.get("interview_at"),
                    digest,
                    now,
                    app_id,
                ),
            )
            outcome = "updated"

    new_events = 0
    for ev in events:
        contact = next(
            (c for c in app.get("contacts", []) if c.get("email") == ev["sender"]), {}
        )
        cur = con.execute(
            "INSERT OR IGNORE INTO events (app_id, msg_id, date, sender, sender_name,"
            " subject, stage, is_from_me) VALUES (?,?,?,?,?,?,?,?)",
            (
                app_id,
                msg_id(app["company"], app["role"], ev),
                ev["date"],
                ev["sender"],
                contact.get("name", ""),
                ev["subject"],
                ev["stage"],
                int(ev.get("from_me", False)),
            ),
        )
        new_events += cur.rowcount
    return app_id, outcome if outcome != "unchanged" or new_events else "unchanged"


def read_application(con: sqlite3.Connection, app_id: int) -> dict | None:
    row = con.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["contacts"] = json.loads(d.get("contacts") or "[]")
    d["events"] = [
        dict(e)
        for e in con.execute(
            "SELECT * FROM events WHERE app_id = ? ORDER BY date, id", (app_id,)
        )
    ]
    d["event_count"] = len(d["events"])
    d["last_activity"] = max((e["date"] for e in d["events"]), default=d.get("applied_on"))
    # Their last word, ignoring anything we sent — this is what ghosting is measured on.
    d["last_inbound"] = max(
        (e["date"] for e in d["events"] if not e["is_from_me"]), default=None
    )
    d["stage_rank"] = STAGE_RANK.get(d["stage"], 0)
    return d


def read_all(con: sqlite3.Connection) -> dict[int, dict]:
    return {
        r["id"]: read_application(con, r["id"])
        for r in con.execute("SELECT id FROM applications")
    }
