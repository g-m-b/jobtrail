"""Manual sync without starting the server. Run: python -m jobtrail.cli sync"""

import argparse
import logging

from .config import Config
from .store import Store
from .sync import run_sync


def main() -> int:
    ap = argparse.ArgumentParser(prog="jobtrail")
    ap.add_argument("command", choices=["sync", "status"])
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    cfg = Config.load(args.config)
    store = Store(cfg.db_path, ghost_days=cfg["rules"]["ghost_days"])

    if args.command == "sync":
        result = run_sync(store, cfg)
        print(result)
        return 0 if result.get("ok") else 1

    s = store.stats()
    print(f"{s['total']} applications | {s['offers']} offers | "
          f"{s['response_rate']}% response | last sync {store.last_sync or 'never'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
