"""Self-checks. Run: python3 tests/test_jobtrail.py  (no pytest needed)"""

import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobtrail import db, providers, sync  # noqa: E402
from jobtrail.config import Config  # noqa: E402
from jobtrail.store import Store  # noqa: E402


def _app(company="Acme", role="Engineer", stage="applied", status="active", events=None):
    return {
        "company": company,
        "role": role,
        "source": "ats",
        "stage": stage,
        "status": status,
        "contacts": [{"name": "R", "email": "r@acme.com"}],
        "events": events
        if events is not None
        else [{"date": "2026-03-01", "stage": "applied", "sender": "r@acme.com", "subject": "Applied"}],
    }


def _cfg(tmp: Path, apps, **over):
    (tmp / "data").mkdir(exist_ok=True)
    (tmp / "data" / "c.jsonl").write_text("\n".join(json.dumps(a) for a in apps))
    data = {
        "sync": {"enabled": False, "interval_hours": 6, "run_on_startup": False, "jitter_seconds": 0},
        "ingest": {"provider": "jsonl", "jsonl": {"path": str(tmp / "data" / "c.jsonl")}},
        "rules": {"ghost_days": 30},
        "server": {"cors_origins": [], "db_path": str(tmp / "jobs.db")},
    }
    for k, v in over.items():
        data[k].update(v)
    cfg = Config(data)
    cfg.resolve = lambda v: Path(v)  # absolute paths in tests
    return cfg


# --- stage model ------------------------------------------------------------

def test_stage_ladder_is_ordered():
    assert db.STAGE_RANK["applied"] < db.STAGE_RANK["interview"] < db.STAGE_RANK["offer"]


def test_derive_takes_furthest_stage_not_latest():
    """A rejection arriving after an interview must not drag the stage back down."""
    events = [
        {"date": "2026-03-01", "stage": "applied"},
        {"date": "2026-03-10", "stage": "interview"},
        {"date": "2026-03-20", "stage": "applied"},
    ]
    applied_on, stage, status = db.derive({"status": "rejected"}, events)
    assert (applied_on, stage, status) == ("2026-03-01", "interview", "rejected")


def test_initiator_inferred_from_first_event():
    assert db.initiator_of([{"date": "2026-03-01", "stage": "recruiter_screen"}]) == "inbound"
    assert db.initiator_of([{"date": "2026-03-01", "stage": "applied"}]) == "outbound"


def test_ghosting_is_derived_not_stored():
    """The clock must keep running between syncs."""
    old = (date.today() - timedelta(days=90)).isoformat()
    fresh = (date.today() - timedelta(days=3)).isoformat()
    assert db.is_ghosted({"status": "active", "last_inbound": old}, 30)
    assert not db.is_ghosted({"status": "active", "last_inbound": fresh}, 30)
    # A stated outcome always beats inferred silence.
    assert not db.is_ghosted({"status": "rejected", "last_inbound": old}, 30)
    # Never heard back at all: fall back to the application date.
    assert db.is_ghosted({"status": "active", "last_inbound": None, "applied_on": old}, 30)


def test_my_own_follow_up_does_not_un_ghost():
    """Chasing a recruiter is not a reply. A follow-up must not reset the clock."""
    old = (date.today() - timedelta(days=90)).isoformat()
    today_s = date.today().isoformat()
    row = {"status": "active", "last_activity": today_s, "last_inbound": old,
           "applied_on": old}
    assert db.is_ghosted(row, 30), "outbound follow-up must not count as a sign of life"
    row["last_inbound"] = today_s
    assert not db.is_ghosted(row, 30), "a real reply does revive it"


# --- sync: diffing and write ordering ---------------------------------------

def test_sync_is_idempotent():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        cfg = _cfg(tmp, [_app()])
        store = Store(cfg.db_path)

        first = sync.run_sync(store, cfg)
        assert first["added"] == 1 and first["new_events"] == 1

        second = sync.run_sync(store, cfg)
        assert second["added"] == 0 and second["updated"] == 0
        assert second["unchanged"] == 1
        # The whole point: an unchanged sync touches nothing.
        assert second["cache_upserts"] == 0
        assert second["new_events"] == 0


def test_sync_upserts_only_changed_rows():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        apps = [_app("A", "r1"), _app("B", "r2"), _app("C", "r3")]
        cfg = _cfg(tmp, apps)
        store = Store(cfg.db_path)
        sync.run_sync(store, cfg)
        version_before = store.version

        # One new event on B only.
        apps[1]["events"].append(
            {"date": "2026-04-01", "stage": "interview", "sender": "r@acme.com", "subject": "Interview"}
        )
        (tmp / "data" / "c.jsonl").write_text("\n".join(json.dumps(a) for a in apps))

        r = sync.run_sync(store, cfg)
        assert r["updated"] == 1 and r["unchanged"] == 2
        assert r["cache_upserts"] == 1, "must refresh only the row that changed"
        assert store.version == version_before + 1
        assert store.applications(company="B")[0]["stage"] == "interview"
        assert store.applications(company="A")[0]["stage"] == "applied"


def test_failed_sync_leaves_cache_intact():
    """A broken feed must not blank the dashboard."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        cfg = _cfg(tmp, [_app()])
        store = Store(cfg.db_path)
        sync.run_sync(store, cfg)
        good = store.applications()
        assert len(good) == 1

        (tmp / "data" / "c.jsonl").write_text("{not json")
        r = sync.run_sync(store, cfg)
        assert r["ok"] is False
        assert store.applications() == good, "cache must survive a provider failure"
        assert store.last_sync_error


def test_manual_edits_survive_sync_but_events_still_land():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        apps = [_app()]
        cfg = _cfg(tmp, apps)
        store = Store(cfg.db_path)
        sync.run_sync(store, cfg)
        app_id = store.applications()[0]["id"]

        store.con.execute(
            "UPDATE applications SET stage='offer', notes='cleared 3 rounds by phone',"
            " manual=1 WHERE id=?",
            (app_id,),
        )
        store.con.commit()
        store.upsert(app_id)

        apps[0]["events"].append(
            {"date": "2026-05-01", "stage": "interview", "sender": "r@acme.com", "subject": "Round 2"}
        )
        (tmp / "data" / "c.jsonl").write_text("\n".join(json.dumps(a) for a in apps))
        sync.run_sync(store, cfg)

        row = store.application(app_id)
        assert row["notes"] == "cleared 3 rounds by phone", "human edit must win"
        assert row["stage"] == "offer", "sync must not walk the stage backwards"
        assert row["event_count"] == 2, "but new email still reaches the timeline"


# --- cache ------------------------------------------------------------------

def test_reads_do_not_touch_the_database():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        cfg = _cfg(tmp, [_app()])
        store = Store(cfg.db_path)
        sync.run_sync(store, cfg)

        store.con.close()  # any read that hits SQLite now raises
        assert len(store.applications()) == 1
        assert store.stats()["total"] == 1
        assert store.contacts()[0]["email"] == "r@acme.com"


def test_ghost_days_change_takes_effect_without_resync():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        old = (date.today() - timedelta(days=45)).isoformat()
        cfg = _cfg(tmp, [_app(events=[{"date": old, "stage": "applied", "sender": "r@acme.com",
                                       "subject": "Applied"}])])
        store = Store(cfg.db_path, ghost_days=30)
        sync.run_sync(store, cfg)
        assert store.applications()[0]["status"] == "ghosted"

        store.ghost_days = 60  # no re-sync, no DB write
        assert store.applications()[0]["status"] == "active"


# --- stats ------------------------------------------------------------------

def test_response_rate_excludes_inbound():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        apps = [
            _app("Out", "r", events=[{"date": "2026-03-01", "stage": "applied",
                                      "sender": "a@x.com", "subject": "s"}]),
            _app("In", "r", events=[{"date": "2026-03-01", "stage": "recruiter_screen",
                                     "sender": "b@x.com", "subject": "s"}]),
        ]
        cfg = _cfg(tmp, apps)
        store = Store(cfg.db_path)
        sync.run_sync(store, cfg)
        s = store.stats()
        assert s["outbound"] == 1 and s["inbound"] == 1
        # 0 of 1 outbound, not 1 of 2 — the recruiter's cold email is not a response.
        assert s["response_rate"] == 0.0


def test_funnel_is_monotonic():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        cfg = _cfg(tmp, [
            _app("A", "r", events=[{"date": "2026-03-01", "stage": "applied", "sender": "a@x.com", "subject": "s"}]),
            _app("B", "r", events=[{"date": "2026-03-02", "stage": "interview", "sender": "b@x.com", "subject": "s"}]),
        ])
        store = Store(cfg.db_path)
        sync.run_sync(store, cfg)
        counts = [f["count"] for f in store.stats()["funnel"]]
        assert counts == sorted(counts, reverse=True), counts
        assert counts[0] == 2


# --- config -----------------------------------------------------------------

def test_env_overrides_config(monkeypatch=None):
    import os

    os.environ["JOBTRAIL_SYNC_INTERVAL_HOURS"] = "2"
    try:
        cfg = Config.load(Path("/nonexistent.toml"))
        assert cfg["sync"]["interval_hours"] == 2
        assert cfg.interval_seconds == 7200
    finally:
        del os.environ["JOBTRAIL_SYNC_INTERVAL_HOURS"]


def test_interval_has_a_floor():
    """A misconfigured 0 must not spin the scheduler into a hot loop."""
    cfg = Config({**{k: dict(v) for k, v in Config.load(Path("/nope.toml"))._data.items()}})
    cfg._data["sync"]["interval_hours"] = 0
    assert cfg.interval_seconds == 60


# --- gmail provider rules ---------------------------------------------------

def test_gmail_classifier_drops_job_board_noise():
    drop = providers._classify(
        "alerts@naukri.com", "✉️ Job | AI Engineer in Hyderabad", "Job invite from recruiter"
    )
    assert drop is None, "job-board blast must not become an application"


def test_gmail_classifier_keeps_real_ats_mail():
    keep = providers._classify(
        "accenture@myworkday.com", "Time to schedule your interview!", "Congrats on making it"
    )
    assert keep and keep[0] == "interview"

    rej = providers._classify(
        "careers@wipro.com", "Application Update", "Unfortunately we regret to inform you"
    )
    assert rej and rej[1] == "rejected"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print(f"  ok    {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
