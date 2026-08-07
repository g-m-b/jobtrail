"""FastAPI app. Reads are served from the cache; writes go to SQLite then upsert it.

Run: uvicorn jobtrail.api:app --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import db, sync
from .config import Config
from .db import STAGES, STATUSES
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

cfg = Config.load()
store = Store(cfg.db_path, ghost_days=cfg["rules"]["ghost_days"])
_stop = asyncio.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if cfg["sync"]["enabled"]:
        task = asyncio.create_task(sync.scheduler(store, cfg, _stop))
        logging.getLogger("jobtrail").info(
            "scheduler on: every %.2fh via '%s'",
            cfg["sync"]["interval_hours"],
            cfg["ingest"]["provider"],
        )
    yield
    _stop.set()
    if task:
        await asyncio.wait([task], timeout=5)


app = FastAPI(title="jobtrail", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg["server"]["cors_origins"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AppPatch(BaseModel):
    company: str | None = None
    role: str | None = None
    source: str | None = None
    stage: str | None = None
    status: str | None = None
    notes: str | None = None
    interview_at: str | None = None


class AppCreate(BaseModel):
    company: str
    role: str
    source: str = "direct"
    stage: str = "applied"
    status: str = "active"
    applied_on: str | None = None
    notes: str = ""
    interview_at: str | None = None


def _validate(stage: str | None, status: str | None) -> None:
    if stage is not None and stage not in STAGES:
        raise HTTPException(422, f"stage must be one of {STAGES}")
    # 'ghosted' is derived from silence, never set by hand.
    if status is not None and status not in STATUSES:
        raise HTTPException(422, f"status must be one of {STATUSES}")


@app.get("/api/applications")
def list_applications(status: str | None = None, company: str | None = None, q: str | None = None):
    return store.applications(status=status, company=company, q=q)


@app.get("/api/applications/{app_id}")
def get_application(app_id: int):
    row = store.application(app_id)
    if row is None:
        raise HTTPException(404, "not found")
    return row


@app.post("/api/applications", status_code=201)
def create_application(body: AppCreate):
    _validate(body.stage, body.status)
    now = datetime.now().isoformat(timespec="seconds")
    try:
        cur = store.con.execute(
            "INSERT INTO applications (company, role, source, applied_on, stage, status,"
            " notes, interview_at, manual, updated_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
            (
                body.company,
                body.role,
                body.source,
                body.applied_on or datetime.now().date().isoformat(),
                body.stage,
                body.status,
                body.notes,
                body.interview_at,
                now,
            ),
        )
        store.con.commit()
    except Exception as e:
        store.con.rollback()
        raise HTTPException(409, f"that company + role already exists ({e})")
    return store.upsert(cur.lastrowid)


@app.patch("/api/applications/{app_id}")
def update_application(app_id: int, body: AppPatch):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(422, "nothing to update")
    _validate(fields.get("stage"), fields.get("status"))
    if store.application(app_id) is None:
        raise HTTPException(404, "not found")

    sets = ", ".join(f"{k} = ?" for k in fields)
    store.con.execute(
        f"UPDATE applications SET {sets}, manual = 1, updated_at = ? WHERE id = ?",
        [*fields.values(), datetime.now().isoformat(timespec="seconds"), app_id],
    )
    store.con.commit()          # DB first
    return store.upsert(app_id)  # then this one cache key


@app.delete("/api/applications/{app_id}", status_code=204)
def delete_application(app_id: int):
    if store.application(app_id) is None:
        raise HTTPException(404, "not found")
    store.con.execute("DELETE FROM events WHERE app_id = ?", (app_id,))
    store.con.execute("DELETE FROM applications WHERE id = ?", (app_id,))
    store.con.commit()
    store.evict(app_id)


@app.get("/api/contacts")
def contacts():
    return store.contacts()


@app.get("/api/upcoming")
def upcoming(limit: int = 10):
    return store.upcoming(limit)


@app.get("/api/stats")
def stats():
    return store.stats()


@app.get("/api/sync")
def sync_status():
    runs = [
        dict(r)
        for r in store.con.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 10")
    ]
    return {
        "enabled": cfg["sync"]["enabled"],
        "provider": cfg["ingest"]["provider"],
        "interval_hours": cfg["sync"]["interval_hours"],
        "last_sync": store.last_sync,
        "last_error": store.last_sync_error,
        "cache_version": store.version,
        "recent_runs": runs,
    }


@app.post("/api/sync")
async def trigger_sync():
    """Force a pull now, without waiting for the next tick."""
    return await asyncio.to_thread(sync.run_sync, store, cfg)
