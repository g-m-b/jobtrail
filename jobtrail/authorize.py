"""One-time Gmail OAuth consent. Run: python -m jobtrail.authorize

Needs an OAuth *desktop* client json from Google Cloud Console (enable the Gmail API,
then Credentials -> Create credentials -> OAuth client ID -> Desktop app).
Scope is read-only: this app never sends, deletes or modifies mail.
"""

import sys

from .config import Config

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main() -> int:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("pip install google-api-python-client google-auth-oauthlib", file=sys.stderr)
        return 1

    cfg = Config.load()
    creds_path = cfg.resolve(cfg["ingest"]["gmail"]["credentials_file"])
    token_path = cfg.resolve(cfg["ingest"]["gmail"]["token_file"])
    if not creds_path.exists():
        print(f"missing {creds_path} — download an OAuth desktop client json", file=sys.stderr)
        return 1

    creds = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES).run_local_server(
        port=0
    )
    token_path.write_text(creds.to_json())
    print(f"wrote {token_path}. Set ingest.provider = \"gmail\" in config.toml.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
