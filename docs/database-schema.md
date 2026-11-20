# JobHelper database schema

SQLite at `data/jobhelper.sqlite` on the server, defined in `jobhelper/db.py` (SQLAlchemy). New tables
are created on startup; there are no migrations. Times are stored as UTC. Row counts are from the
live server on 2026-11-14.

```
jobs 1 ──< applications 1 ──< events
  │                              │
  └──────────< events (job_id) ──┘
company_ratings  (matched to jobs.company by normalised name, no foreign key)
emails_seen · site_logins · llm_usage · rate_limits · runs · kb_fts   (standalone)
```

## jobs
One posting found by any source. Unique on (`source`, `external_id`).

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| source | VARCHAR(32), indexed | naukri, linkedin, indeed, greenhouse, lever, ashby, email |
| external_id | VARCHAR(128) | the source's own id |
| url | TEXT | posting page |
| apply_url | TEXT, null | company site, when the portal redirects |
| title | VARCHAR(300) | |
| company | VARCHAR(200), indexed | |
| location | VARCHAR(300), null | |
| remote | BOOLEAN | |
| experience | VARCHAR(64), null | e.g. "5-9 Yrs" |
| salary | VARCHAR(128), null | |
| description | TEXT, null | |
| skills | JSON | list |
| posted_at | DATETIME, null | |
| discovered_at | DATETIME, indexed | |
| via | VARCHAR(64), null | search, alert:naukri-priority, board |
| priority | BOOLEAN | came from a Naukri Priority Applicant alert |
| easy_apply | BOOLEAN, null | |
| status | VARCHAR(24), indexed | new, prefiltered, filtered_out, review, queued_auto, approved, applied, needs_user, apply_yourself, skipped, dismissed, failed |
| filter_reason | VARCHAR(300), null | why the prefilter dropped it (title, location, age, company rated 1) |
| score | FLOAT, indexed, null | 0-100 fit score |
| score_detail | JSON | summary, reasons, gaps, red_flags, location_ok, verdict |
| scored_at | DATETIME, null | |
| resume_variant | VARCHAR(200), null | resume file chosen at scoring |
| reviewed_at | DATETIME, null | |
| review_note | TEXT, null | your note, or why it went to review |

## applications
One attempt to apply to a job.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| job_id | INTEGER → jobs.id, indexed | |
| portal | VARCHAR(32), indexed | counts against this portal's daily cap |
| mode | VARCHAR(16) | auto, approved |
| status | VARCHAR(24), indexed | queued, applying, applied, awaiting_review, dry_run_ok, review_expired, needs_user, apply_yourself, already_applied, not_eligible, failed, skipped_by_user, then replies: viewed, shortlisted, assessment, interview, offer, rejected |
| attempts | INTEGER | agent runs so far |
| created_at / started_at / applied_at / last_update_at | DATETIME | applied_at indexed |
| final_url | TEXT, null | last page the agent was on |
| notes | TEXT, null | evidence and history |
| blocker | TEXT, null | what it's waiting on you for |
| answers | JSON | [{question, answer, source}] the agent filled in |
| artifacts | JSON | screenshot / transcript paths |

## events
Timeline for jobs and applications: email replies, status changes, blockers, your answers.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| application_id | INTEGER → applications.id, indexed, null | |
| job_id | INTEGER → jobs.id, indexed, null | |
| kind | VARCHAR(32), indexed | e.g. apply_applied, apply_needs_user, resumed, mail_interview, too_old, skipped, assistant_action |
| summary | TEXT | |
| detail | JSON | e.g. the question + options for needs_user, the parsed email |
| at | DATETIME, indexed | |

## company_ratings
AmbitionBox ratings, rated 1 / 3 / 5 (see `jobhelper/companies.py`).

| Column | Type | Notes |
|---|---|---|
| name | VARCHAR(200) PK | normalised company name as it appears in postings |
| overall | INTEGER, null | 1, 3 or 5; null = too few reviews / no match |
| detail | JSON | scores {culture, benefits, work, work_life}, raw values, AmbitionBox url, matched name, review count |
| fetched_at | DATETIME | refreshed after 30 days (1 day after an error) |

## emails_seen
Gmail messages already handled, so none is processed twice.

| Column | Type | Notes |
|---|---|---|
| message_id | VARCHAR(300) PK | |
| kind | VARCHAR(24) | alert, tracking, ignored |
| subject / sender | TEXT / VARCHAR(300), null | |
| at | DATETIME | |
| result | JSON | what the triage decided |

## site_logins
Career-site accounts the agent created. Passwords are Fernet-encrypted.

| Column | Type | Notes |
|---|---|---|
| domain | VARCHAR(200) PK | |
| username | VARCHAR(200) | |
| password_enc | TEXT | encrypted |
| created_at | DATETIME | |
| note | TEXT, null | |

## llm_usage
One Claude call or agent session and what it used (shared subscription).

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| at | DATETIME, indexed | |
| task | VARCHAR(24), indexed | scoring, apply, email, prep, assistant, health |
| model | VARCHAR(64), null | |
| input_tokens / output_tokens / cache_read_tokens / cache_write_tokens | INTEGER | |
| cost_usd | FLOAT | API-price equivalent; you pay via the subscription |
| turns / duration_ms | INTEGER | |
| ref | VARCHAR(64), null | e.g. "app-12", "batch" |
| ok | BOOLEAN | |

## rate_limits (1 row)
Latest subscription-limit reading per window.

| Column | Type | Notes |
|---|---|---|
| limit_type | VARCHAR(32) PK | five_hour, seven_day, ... |
| status | VARCHAR(24) | allowed, allowed_warning, rejected |
| utilization | FLOAT, null | fraction of the window used |
| resets_at / updated_at | DATETIME | |

## runs
Scheduled and manual runs with their stats.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| kind | VARCHAR(32), indexed | discover, score, apply, email, summary |
| started_at / finished_at | DATETIME | |
| ok | BOOLEAN, null | |
| stats | JSON | per-run counts and results |
| error | TEXT, null | |

## kb_fts
SQLite FTS5 full-text index over the knowledge base (`data/kb/*.md`), rebuilt by `kb.reindex()`.

`CREATE VIRTUAL TABLE kb_fts USING fts5(path UNINDEXED, title, body, tags, tokenize='porter unicode61')`

## Kept outside the database (in `data/`)
`profile.yaml` (your profile), `overrides.yaml` (dashboard settings), `pause.json`, `telegram.json`,
`kb/` (knowledge base files), `resumes/`, `artifacts/` (screenshots, agent transcripts).
