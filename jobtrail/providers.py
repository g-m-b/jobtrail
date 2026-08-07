"""Ingest providers.

A provider is just a callable: `(config) -> list[application dict]`, in the schema
documented in `docs/ingest-format.md`. Register your own with @provider("name") and
select it with `ingest.provider` in config.toml — that is the whole extension point.

Two ship in the box:
  jsonl  — read a classified file. Zero credentials; an LLM agent writes the file.
  gmail  — talk to the Gmail API directly. Self-syncing, no agent in the loop.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

REGISTRY: dict[str, Callable] = {}


def provider(name: str):
    def wrap(fn):
        REGISTRY[name] = fn
        return fn

    return wrap


def get(name: str) -> Callable:
    if name not in REGISTRY:
        raise ValueError(f"unknown ingest provider {name!r}; have {sorted(REGISTRY)}")
    return REGISTRY[name]


# --------------------------------------------------------------------------- jsonl


@provider("jsonl")
def from_jsonl(cfg) -> list[dict]:
    path = cfg.resolve(cfg["ingest"]["jsonl"]["path"])
    if not path.exists():
        return []
    out = []
    for line_no, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path.name}:{line_no}: {e}") from e
        if rec.get("is_application") is False:
            continue
        if not rec.get("company") or not rec.get("role"):
            raise ValueError(f"{path.name}:{line_no}: needs both 'company' and 'role'")
        out.append(rec)
    return out


# --------------------------------------------------------------------------- gmail

# Senders that are applicant-tracking systems: mail from here is about a real application.
ATS_DOMAINS = (
    "myworkday.com", "lever.co", "greenhouse.io", "ashbyhq.com", "smartrecruiters.com",
    "icims.com", "njoyn.com", "taleo.net", "jobvite.com", "successfactors.com",
    "workablemail.com", "eightfold.ai", "phenompeople.com", "hire.lever.co",
)

# Job-board blasts that look like applications but are not. Ordered before the
# positive rules because "✉️ Job | AI Engineer" trips almost every keyword.
NOISE_SENDERS = (
    "naukri.com", "match.indeed.com", "cutshort.io", "foundit.in", "shine.com",
    "messages-noreply@linkedin.com", "em.linkedin.com", "naukrialerts",
    "donotreply_mailer", "talent500.co",
)
NOISE_SUBJECTS = re.compile(
    r"(job invite|walk-?in invite|viewed your profile|jobs? in your inbox|"
    r"is hiring|check out jobs|job digest|are hiring|saved job)",
    re.I,
)

STAGE_PATTERNS = [
    ("offer", re.compile(r"\b(offer letter|congratulations on your offer|onboarding|"
                         r"letter of intent|bgv|background check)\b", re.I)),
    ("final", re.compile(r"\b(final round|hr round|cleared (the )?interview|"
                         r"documents? required|selected for)\b", re.I)),
    ("interview", re.compile(r"\b(interview|technical evaluation|l[0-3] discussion|"
                             r"schedule your|slot|round \d)\b", re.I)),
    ("assessment", re.compile(r"\b(assessment|online test|hackerrank|codility|"
                              r"coding challenge|aptitude)\b", re.I)),
    ("recruiter_screen", re.compile(r"\b(job description|jd |opportunity|"
                                    r"your profile|shortlist|screening)\b", re.I)),
    ("acknowledged", re.compile(r"\b(thank you for applying|application received|"
                                r"received your application|application successful|"
                                r"thanks for applying|acknowledgement)\b", re.I)),
]
REJECTED = re.compile(
    r"(unfortunately|regret to inform|not (be )?(moving|proceeding) forward|"
    r"did not select|other candidates|no longer under consideration|not selected)", re.I
)
# Rejection language alone is NOT enough: order and delivery mail says "we regret to
# inform you" constantly. Require some hiring context before believing it.
JOB_CONTEXT = re.compile(
    r"\b(applicat|candidat|role|position|interview|resume|cv|hiring|recruit|vacancy|"
    r"job|career|offer letter|onboard)", re.I
)
# "Interview is scheduled for 2026-07-29 05:00 PM IST" / "on Thu, March 12, 4:00 PM"
WHEN = re.compile(r"(\d{4}-\d{2}-\d{2})[ T]+(\d{1,2}):(\d{2})\s*(AM|PM)?", re.I)


def _company_from(sender: str, subject: str) -> str:
    m = re.search(r"(?:application (?:to|was sent to)|applying to|interest in)\s+(.+?)(?:[!.,]|$)",
                  subject, re.I)
    if m:
        return m.group(1).strip()[:60]
    domain = sender.split("@")[-1].lower()
    for strip in ("mail.", "email.", "no-reply.", "noreply.", "careers.", "hire."):
        domain = domain.removeprefix(strip)
    return domain.split(".")[0].replace("-", " ").title()


def _classify(sender: str, subject: str, snippet: str) -> tuple[str, str] | None:
    blob = f"{subject} {snippet}"
    low = sender.lower()
    if any(n in low for n in NOISE_SENDERS) and not any(a in low for a in ATS_DOMAINS):
        return None
    if NOISE_SUBJECTS.search(subject) and not any(a in low for a in ATS_DOMAINS):
        return None
    job_ish = bool(JOB_CONTEXT.search(blob))
    rejected = bool(REJECTED.search(blob)) and job_ish
    status = "rejected" if rejected else "active"

    for stage, pat in STAGE_PATTERNS:
        if pat.search(blob) and (job_ish or any(a in low for a in ATS_DOMAINS)):
            return stage, status
    if any(a in low for a in ATS_DOMAINS):
        return "applied", status
    # A rejection is itself proof an application existed, even when no other rule
    # placed it — but only once the job-context gate above has passed, so that
    # "we regret to inform you about your order" stays out.
    if rejected:
        return "applied", "rejected"
    return None


@provider("gmail")
def from_gmail(cfg) -> list[dict]:
    """Pull straight from the Gmail API. Needs a one-time OAuth consent:

        pip install google-api-python-client google-auth-oauthlib
        # download an OAuth *desktop* client json from Google Cloud Console
        python -m jobtrail.authorize

    Classification here is rules-based on purpose — it runs unattended every few hours
    and must not need an LLM key. It is deliberately more conservative than an agent:
    it drops anything it cannot place, so expect it to miss the long tail of recruiters
    mailing from personal addresses. Correct those in the UI; manual edits survive sync.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "gmail provider needs: pip install google-api-python-client google-auth-oauthlib"
        ) from e

    gcfg = cfg["ingest"]["gmail"]
    token_path = cfg.resolve(gcfg["token_file"])
    if not token_path.exists():
        raise RuntimeError(
            f"no Gmail token at {token_path}. Run: python -m jobtrail.authorize"
        )
    creds = Credentials.from_authorized_user_file(
        str(token_path), ["https://www.googleapis.com/auth/gmail.readonly"]
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())

    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    after = (datetime.now(timezone.utc) - timedelta(days=int(gcfg["lookback_days"]))).strftime(
        "%Y/%m/%d"
    )
    query = f"after:{after} -in:chats {gcfg.get('extra_query', '')}".strip()

    ids, page = [], None
    while len(ids) < int(gcfg["max_results"]):
        resp = (
            svc.users()
            .messages()
            .list(userId="me", q=query, pageToken=page, maxResults=100)
            .execute()
        )
        ids += [m["id"] for m in resp.get("messages", [])]
        page = resp.get("nextPageToken")
        if not page:
            break

    grouped: dict[tuple[str, str], dict] = {}
    for mid in ids[: int(gcfg["max_results"])]:
        msg = (
            svc.users()
            .messages()
            .get(userId="me", id=mid, format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        sender = (re.search(r"[\w.+-]+@[\w.-]+", headers.get("from", "")) or [""])[0] \
            if re.search(r"[\w.+-]+@[\w.-]+", headers.get("from", "")) else headers.get("from", "")
        subject = headers.get("subject", "")
        snippet = msg.get("snippet", "")
        verdict = _classify(sender, subject, snippet)
        if not verdict:
            continue
        stage, status = verdict
        when = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, timezone.utc)
        company = _company_from(sender, subject)
        role = (re.search(r"for (?:the )?(?:position of |role of )?([A-Z][\w /&+-]{3,50})",
                          f"{subject} {snippet}") or [None, "Unknown role"])[1]

        key = (company.lower(), str(role).strip().lower())
        app = grouped.setdefault(
            key,
            {
                "company": company,
                "role": str(role).strip(),
                "source": "ats" if any(a in sender.lower() for a in ATS_DOMAINS) else "direct",
                "status": "active",
                "contacts": [{"name": "", "email": sender}],
                "events": [],
            },
        )
        if status == "rejected":
            app["status"] = "rejected"
        m = WHEN.search(f"{subject} {snippet}")
        if m and stage == "interview":
            app["interview_at"] = f"{m.group(1)}T{int(m.group(2)):02d}:{m.group(3)}:00"
        app["events"].append(
            {
                "date": when.date().isoformat(),
                "stage": stage,
                "sender": sender,
                "subject": subject,
                "from_me": False,
            }
        )
    return list(grouped.values())
