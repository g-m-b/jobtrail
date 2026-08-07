"""Sync orchestration and the interval scheduler.

Write ordering is the whole point of this module:

    provider  ->  diff  ->  SQLite (durable)  ->  cache upsert (only what changed)

The cache is never cleared. A run that touches 2 of 400 applications re-reads exactly
those 2 rows; the other 398 stay hot. If the DB write fails, the cache is left alone,
so readers keep serving the last known-good data rather than an empty page.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime

from . import db, providers
from .store import Store

log = logging.getLogger("jobtrail.sync")


class SyncResult(dict):
    """dict so it serialises straight to JSON from the API."""

    def __str__(self) -> str:
        if not self.get("ok"):
            return f"sync failed: {self.get('error')}"
        return (
            f"{self['provider']}: {self['added']} added, {self['updated']} updated, "
            f"{self['new_events']} new events, {self['unchanged']} unchanged "
            f"({self['duration_ms']}ms)"
        )


def run_sync(store: Store, cfg) -> SyncResult:
    """One pull. Safe to call concurrently — serialised on the store lock."""
    name = cfg["ingest"]["provider"]
    started = datetime.now()
    run_id = store.con.execute(
        "INSERT INTO sync_runs (started, provider) VALUES (?,?)",
        (started.isoformat(timespec="seconds"), name),
    ).lastrowid
    store.con.commit()

    try:
        apps = providers.get(name)(cfg)
    except Exception as e:  # provider blew up — cache and DB both untouched
        log.exception("provider %s failed", name)
        store.last_sync_error = str(e)
        store.con.execute(
            "UPDATE sync_runs SET finished=?, ok=0, error=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), str(e), run_id),
        )
        store.con.commit()
        return SyncResult(ok=False, provider=name, error=str(e))

    added = updated = unchanged = 0
    touched: list[int] = []
    before_events = store.con.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    with store._lock:
        try:
            for app in apps:
                app_id, outcome = db.upsert_application(store.con, app)
                if outcome == "added":
                    added += 1
                    touched.append(app_id)
                elif outcome == "updated":
                    updated += 1
                    touched.append(app_id)
                else:
                    unchanged += 1
            store.con.commit()          # durable FIRST
        except Exception as e:
            store.con.rollback()        # cache never saw it, so nothing to undo
            log.exception("sync write failed")
            store.last_sync_error = str(e)
            store.con.execute(
                "UPDATE sync_runs SET finished=?, ok=0, error=? WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), str(e), run_id),
            )
            store.con.commit()
            return SyncResult(ok=False, provider=name, error=str(e))

        store.upsert_many(touched)      # cache SECOND, and only the changed rows

    new_events = store.con.execute("SELECT COUNT(*) FROM events").fetchone()[0] - before_events
    finished = datetime.now()
    store.last_sync = finished.isoformat(timespec="seconds")
    store.last_sync_error = None
    store.con.execute(
        "UPDATE sync_runs SET finished=?, added=?, updated=?, events=?, ok=1 WHERE id=?",
        (finished.isoformat(timespec="seconds"), added, updated, new_events, run_id),
    )
    store.con.commit()

    result = SyncResult(
        ok=True,
        provider=name,
        added=added,
        updated=updated,
        unchanged=unchanged,
        new_events=new_events,
        cache_upserts=len(touched),
        duration_ms=int((finished - started).total_seconds() * 1000),
        at=store.last_sync,
    )
    log.info("%s", result)
    return result


async def scheduler(store: Store, cfg, stop: asyncio.Event) -> None:
    """Fixed-interval loop.

    ponytail: a plain asyncio loop, not APScheduler — "every N hours" needs no cron
    parser, no job store, no extra dependency. Swap in APScheduler the day you need
    real cron expressions or misfire handling.
    """
    interval = cfg.interval_seconds
    jitter = float(cfg["sync"].get("jitter_seconds", 0) or 0)

    if cfg["sync"].get("run_on_startup", True):
        await asyncio.to_thread(run_sync, store, cfg)

    while not stop.is_set():
        delay = interval + random.uniform(0, jitter)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return  # shutdown requested
        except asyncio.TimeoutError:
            pass
        try:
            # to_thread: the provider does blocking IO and must not stall the event loop.
            await asyncio.to_thread(run_sync, store, cfg)
        except Exception:
            # Never let one bad run kill the loop — it would silently stop syncing.
            log.exception("scheduled sync raised; continuing")
