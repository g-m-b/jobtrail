"""Configuration: TOML file + environment overrides.

TOML rather than YAML because tomllib is stdlib — one less dependency for anyone
cloning this.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "sync": {
        "enabled": True,
        "interval_hours": 6,
        "run_on_startup": True,
        "jitter_seconds": 30,
    },
    "ingest": {
        "provider": "jsonl",
        "jsonl": {"path": "data/classified.jsonl"},
        "gmail": {
            "credentials_file": "credentials.json",
            "token_file": "token.json",
            "lookback_days": 180,
            "max_results": 500,
            "extra_query": "",
        },
    },
    "rules": {"ghost_days": 30},
    "server": {"cors_origins": ["http://localhost:5173"], "db_path": "jobs.db"},
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def _coerce(default: Any, raw: str) -> Any:
    """Env vars arrive as strings; match the type of the default we're replacing."""
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        # Keep fractions: interval_hours defaults to an int, but "0.5" must stay 0.5
        # rather than truncating to 0 and silently disabling the documented behaviour.
        val = float(raw)
        return int(val) if val.is_integer() else val
    if isinstance(default, list):
        return [p.strip() for p in raw.split(",") if p.strip()]
    return raw


class Config:
    def __init__(self, data: dict, path: Path | None = None):
        self._data = data
        self.path = path

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path) if path else ROOT / (os.getenv("JOBTRAIL_CONFIG") or "config.toml")
        data = DEFAULTS
        if path.exists():
            with open(path, "rb") as fh:
                data = _merge(DEFAULTS, tomllib.load(fh))

        # JOBTRAIL_SYNC_INTERVAL_HOURS -> ["sync"]["interval_hours"]
        for section, values in list(data.items()):
            if not isinstance(values, dict):
                continue
            for key, default in values.items():
                if isinstance(default, dict):
                    continue
                raw = os.getenv(f"JOBTRAIL_{section}_{key}".upper())
                if raw is not None:
                    values[key] = _coerce(default, raw)

        return cls(data, path if path.exists() else None)

    def __getitem__(self, section: str) -> dict:
        return self._data[section]

    def resolve(self, value: str) -> Path:
        """Relative paths in config are relative to the repo root, not the cwd —
        otherwise the scheduler resolves them differently depending on where it started."""
        p = Path(value).expanduser()
        return p if p.is_absolute() else ROOT / p

    @property
    def interval_seconds(self) -> float:
        return max(60.0, float(self["sync"]["interval_hours"]) * 3600)

    @property
    def db_path(self) -> Path:
        return self.resolve(self["server"]["db_path"])
