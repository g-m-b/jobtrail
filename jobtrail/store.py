"""In-memory read cache over SQLite.

Contract: **the database is written first, then the cache is upserted from it.**
The cache is never rebuilt wholesale on a write — a sync that changes 2 of 400
applications re-reads exactly those 2 rows. Reads never touch SQLite at all, so a
6-hourly sync costs 2 row reads rather than a full reload.

ponytail: single-process dict. At 97 rows SQLite alone is already fast enough — this
earns its keep in the thousands, or once reads get expensive. If you run multiple
uvicorn workers, each gets its own copy and a write in one is invisible to the others
until its next sync; move to Redis (or run one worker) before scaling out.
"""

from __future__ import annotations

import statistics
import threading
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from . import db
from .db import STAGE_RANK, STAGES


class Store:
    def __init__(self, db_path: Path | str, ghost_days: int = 30):
        self.db_path = db_path
        self.ghost_days = ghost_days
        self.con = db.connect(db_path)
        db.init(self.con)
        # ponytail: one lock for the whole cache. Writes are rare (a sync every few
        # hours); per-key locking would be more code for no measurable gain.
        self._lock = threading.RLock()
        self._apps: dict[int, dict] = {}
        self.version = 0
        self.last_sync: str | None = None
        self.last_sync_error: str | None = None
        self._restore_sync_state()
        self.reload()

    def _restore_sync_state(self) -> None:
        """A fresh process must not report 'never synced' when the DB knows better."""
        row = self.con.execute(
            "SELECT finished, ok, error FROM sync_runs WHERE finished IS NOT NULL"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            self.last_sync = row["finished"] if row["ok"] else self.last_sync
            self.last_sync_error = None if row["ok"] else row["error"]

    # ---- cache maintenance -------------------------------------------------

    def reload(self) -> int:
        """Full read. Startup only — a sync uses upsert(), not this."""
        with self._lock:
            self._apps = db.read_all(self.con)
            self.version += 1
            return len(self._apps)

    def upsert(self, app_id: int) -> dict | None:
        """Refresh exactly one application from the DB. This is the write path."""
        with self._lock:
            row = db.read_application(self.con, app_id)
            if row is None:
                self._apps.pop(app_id, None)
            else:
                self._apps[app_id] = row
            self.version += 1
            return row

    def upsert_many(self, app_ids) -> int:
        ids = list(dict.fromkeys(app_ids))
        for app_id in ids:
            self.upsert(app_id)
        return len(ids)

    def evict(self, app_id: int) -> None:
        with self._lock:
            self._apps.pop(app_id, None)
            self.version += 1

    # ---- reads (never hit SQLite) -----------------------------------------

    def _decorate(self, row: dict, today: date | None = None) -> dict:
        """Ghosting is applied here, not stored, so it is always current — an
        application does not sit at 'active' forever just because nothing re-synced."""
        out = dict(row)
        if db.is_ghosted(row, self.ghost_days, today):
            out["status"] = "ghosted"
        return out

    def applications(self, status=None, company=None, q=None) -> list[dict]:
        today = date.today()
        with self._lock:
            rows = [self._decorate(r, today) for r in self._apps.values()]
        if status:
            rows = [r for r in rows if r["status"] == status]
        if company:
            rows = [r for r in rows if r["company"] == company]
        if q:
            n = q.lower()
            rows = [
                r
                for r in rows
                if n in r["company"].lower()
                or n in r["role"].lower()
                or n in (r.get("notes") or "").lower()
            ]
        rows.sort(key=lambda r: (r.get("applied_on") or ""), reverse=True)
        return rows

    def application(self, app_id: int) -> dict | None:
        with self._lock:
            row = self._apps.get(app_id)
        return self._decorate(row) if row else None

    def contacts(self) -> list[dict]:
        out: dict[str, dict] = {}
        for r in self.applications():
            for c in r.get("contacts") or []:
                email = (c.get("email") or "").lower()
                if not email:
                    continue
                entry = out.setdefault(
                    email,
                    {"email": email, "name": c.get("name", ""), "applications": [], "last_contact": ""},
                )
                entry["name"] = entry["name"] or c.get("name", "")
                entry["applications"].append(
                    {"id": r["id"], "company": r["company"], "role": r["role"], "status": r["status"]}
                )
                entry["last_contact"] = max(entry["last_contact"], r.get("last_activity") or "")
        return sorted(out.values(), key=lambda c: c["last_contact"], reverse=True)

    def upcoming(self, limit: int = 10) -> list[dict]:
        """Scheduled interviews still in the future — the 'what's next' panel."""
        now = datetime.now().isoformat(timespec="seconds")
        rows = [r for r in self.applications() if (r.get("interview_at") or "") >= now]
        rows.sort(key=lambda r: r["interview_at"])
        return rows[:limit]

    def stats(self) -> dict:
        rows = self.applications()
        total = len(rows)
        ranked = [STAGE_RANK.get(r["stage"], 0) for r in rows]

        funnel = [
            {"stage": s, "count": sum(1 for k in ranked if k >= STAGE_RANK[s])}
            for s in STAGES
            if s != "acknowledged"
        ]
        interviewed = sum(1 for k in ranked if k >= STAGE_RANK["interview"])
        offers = sum(1 for r in rows if r["status"] == "offer" or r["stage"] == "offer")

        # Response rate is meaningful only for applications the user sent. A recruiter's
        # cold email is not a response to anything.
        outbound = [(r, k) for r, k in zip(rows, ranked) if r["initiator"] == "outbound"]
        inbound = [(r, k) for r, k in zip(rows, ranked) if r["initiator"] == "inbound"]
        responded = sum(1 for _, k in outbound if k >= STAGE_RANK["acknowledged"])
        n_out = len(outbound)

        by_source: dict[str, dict] = defaultdict(
            lambda: {"total": 0, "responded": 0, "interviewed": 0}
        )
        for r, k in outbound:
            s = by_source[r["source"]]
            s["total"] += 1
            s["responded"] += k >= STAGE_RANK["acknowledged"]
            s["interviewed"] += k >= STAGE_RANK["interview"]

        by_month: dict[str, Counter] = defaultdict(Counter)
        for r in rows:
            if r.get("applied_on"):
                by_month[r["applied_on"][:7]][r["status"]] += 1

        gaps = []
        for r in rows:
            if r["initiator"] != "outbound" or not r.get("applied_on"):
                continue
            # Past 'acknowledged': an ATS auto-reply lands the same second you submit,
            # so counting it would make the median 0 days and say nothing.
            first = min(
                (
                    e["date"]
                    for e in r["events"]
                    if not e["is_from_me"] and e["stage"] not in ("applied", "acknowledged")
                ),
                default=None,
            )
            if first and first >= r["applied_on"]:
                d0 = datetime.strptime(r["applied_on"], "%Y-%m-%d")
                gaps.append((datetime.strptime(first, "%Y-%m-%d") - d0).days)

        return {
            "total": total,
            "outbound": n_out,
            "inbound": len(inbound),
            "responded": responded,
            "interviewed": interviewed,
            "inbound_interviewed": sum(1 for _, k in inbound if k >= STAGE_RANK["interview"]),
            "offers": offers,
            "active": sum(1 for r in rows if r["status"] == "active"),
            "ghosted": sum(1 for r in rows if r["status"] == "ghosted"),
            "rejected": sum(1 for r in rows if r["status"] == "rejected"),
            "response_rate": round(100 * responded / n_out, 1) if n_out else 0,
            "interview_rate": round(100 * interviewed / total, 1) if total else 0,
            "median_days_to_response": round(statistics.median(gaps), 1) if gaps else None,
            "funnel": funnel,
            "by_source": [
                {
                    "source": k,
                    **v,
                    "response_rate": round(100 * v["responded"] / v["total"], 1),
                    "interview_rate": round(100 * v["interviewed"] / v["total"], 1),
                }
                for k, v in sorted(by_source.items(), key=lambda kv: -kv[1]["total"])
            ],
            "by_month": [
                {"month": m, **dict(c), "total": sum(c.values())}
                for m, c in sorted(by_month.items())
            ],
            "by_status": dict(Counter(r["status"] for r in rows)),
            "cache": {
                "version": self.version,
                "cached_applications": len(self._apps),
                "last_sync": self.last_sync,
                "last_sync_error": self.last_sync_error,
            },
        }
