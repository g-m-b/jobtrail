# Agent sync recipe (the `jsonl` provider)

How to have an LLM agent with mailbox access classify your inbox into
`data/classified.jsonl`. Use this when you want better classification than the
rules-based `gmail` provider can manage — an agent reads context a regex can't, which is
where recruiters mailing from personal Gmail addresses live.

Point your agent at this file:

> "Re-sync my job applications using docs/agent-sync-recipe.md"

Then the scheduler picks the file up on its next tick (or run `python3 -m jobtrail.cli sync`).

## Step 1 — Filter mailbox-side

Let the mail provider do the filtering. Don't download thousands of messages to discard
most of them. Run these, dedupe by thread id.

Two facts that these queries depend on, and that cost real debugging to find:

- **Subject + sender + snippet is enough to classify.** Full bodies are ~15KB of HTML
  wrapper around ~1KB of text. Only fetch a body when you need an interview date.
- **Don't blanket-exclude a promotions/unsubscribe label.** LinkedIn's "your application
  was sent to X" confirmations carry it. Exclude it only in the broad keyword queries.

```
A  ATS senders (no label exclusion)
   newer_than:6m {from:myworkday.com from:lever.co from:greenhouse.io from:ashbyhq.com
   from:smartrecruiters.com from:icims.com from:njoyn.com from:taleo.net from:jobvite.com
   from:successfactors.com from:workablemail.com from:eightfold.ai from:phenompeople.com}

B  LinkedIn, applications only (no label exclusion)
   newer_than:6m from:jobs-noreply@linkedin.com
   {subject:"your application was sent to" subject:"your application to"}

C  Generic phrases, job-board noise excluded
   newer_than:6m -label:<your-unsubscribe-label-id> -from:naukri.com -from:indeed.com
   -from:linkedin.com -from:shine.com -from:cutshort.io -from:foundit.in
   {subject:"applying" subject:"application" subject:"interview" subject:"assessment"
   subject:"shortlisted" subject:"offer letter" subject:"selected" subject:"round"}

D  Threads you replied to — the strongest signal in any mailbox
   newer_than:6m in:sent {"CTC" "notice period" "resume" "PFA" "interview" "opportunity" "JD"}

E  Rejection language
   newer_than:6m -label:<your-unsubscribe-label-id>
   {"unfortunately" "regret to inform" "not to move forward" "did not select"}
```

Large results may overflow to a file. Read them compactly rather than dumping JSON:

```sh
jq -r '.threads[] | .id as $t | .messages[] |
       [$t, (.date|.[0:10]), .sender, (.subject//"")] | @tsv' <overflow-file>
```

## Step 2 — Classify

Read each thread in batches of ~25 and emit one JSON line per **application** (a company +
role pair), not per email. Schema and stage ladder: `docs/ingest-format.md`.

Drop anything that isn't an application the person actually engaged with:

- job-board blasts — naukri "✉️ Job |", Indeed "Role @ Company", cutshort/foundit digests
- LinkedIn network noise — "who viewed your profile", "X is hiring"
- newsletters, shopping, and **bank/credit-card applications** — "your application is
  approved" from a bank is not a job

Keep recruiter-agency threads: they are real processes. Record the **client** as `company`
and the agency in `source` — when a staffing firm submits you to a large consultancy, the
consultancy is the company and `source` is `agency`.

Set `interview_at` whenever a date and time appear — that drives `/api/upcoming`.

## Step 3 — Load

```sh
python3 -m jobtrail.cli sync
```

Idempotent. Records are content-hashed, so re-running with unchanged data performs zero
writes. Rows edited in the UI keep their stage, status and notes; new events still append
to their timeline.
