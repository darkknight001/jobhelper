# Finding your way around the code

A guide for reading and changing JobHelper. For *why* things are built this way see
[DESIGN.md](DESIGN.md); for diagrams see [architecture.md](architecture.md).

## Start here: six files, in this order

Read these and the rest of the code will make sense.

1. **`config/settings.yaml`** (copied from `settings.example.yaml`; `config.settings_file()` falls back to the example): every knob: target roles, locations, keywords, thresholds, caps,
   schedules, models. Most behaviour changes start and end here.
2. **`jobhelper/db.py`**: the tables. `Job`, `Application` and `Event` carry the whole story.
3. **`jobhelper/pipeline.py`**: how a posting gets in: gather → filter → store → score.
4. **`jobhelper/scoring.py`**: how Claude scores a batch and `route()` decides auto / review / skip.
5. **`jobhelper/apply/engine.py`**: the apply queue: caps, pacing, and turning an agent run into state.
6. **`jobhelper/apply/agent.py`**: one Claude run per application, and the guard that fences it in.

## The repo

| Path | What's there |
|---|---|
| `jobhelper/` | All the Python. One package, no sub-services. |
| `jobhelper/web/` | Dashboard (`app.py`), Hermes API (`api.py`), templates, CSS, the chat script |
| `jobhelper/apply/` | Applying: queue (`engine`), agent run (`agent`), agent tools (`tools`), site logins (`vault`) |
| `jobhelper/sources/` | One file per job source, all producing `base.Posting` |
| `config/` | `settings.example.yaml` (committed; your own `settings.yaml` is gitignored), `profile.example.yaml`, `claims-policy.example.md` |
| `.claude/skills/` | The four SKILL.md files. The harness uses them as its prompts (`skills.body()`). |
| `tests/` | pytest, no network. `conftest.py` points everything at a temp data dir. |
| `deploy/` | Dockerfile, supervisord (Xvfb, VNC, noVNC, app), Caddy site |
| `scripts/` | `setup.sh` (first-time setup), `deploy.sh` (ship to the server), `package_skills.py` (skills for claude.ai) |
| `hermes/` | Hermes skill + webhook route, installed on the server |
| `docs/` | `SETUP.md` (install walk-through), this guide, design, architecture, database schema |
| `data/` | Runtime state. Never committed, never deployed (it lives on the server). |

## The package, by layer

Each layer calls the ones below it.

| Layer | Modules |
|---|---|
| **Interfaces** (what you touch) | `web/app.py` dashboard · `web/api.py` Hermes API · `telegram_bot.py` · `assistant.py` chat · `cli.py` |
| **Orchestration** (when, in what order) | `scheduler.py` · `pipeline.py` · `apply/engine.py` · `pause.py` |
| **Judgement** (Claude) | `scoring.py` · `companies.py` · `apply/agent.py` · `apply/tools.py` · `tracking.py` · `prep.py` · `llm.py` · `usage.py` |
| **Status** (no Claude) | `portal_status.py`: Naukri history + LinkedIn job tracker |
| **Integrations** (outside world) | `sources/*` · `mail.py` · `browser.py` · `kb.py` · `notify.py` · `apply/vault.py` |
| **Foundation** | `config.py` · `db.py` · `summary.py` · `skills.py` · `verify.py` · `prefilter.py` |

## Follow a job through the code

1. **Found.** `scheduler.build()` runs `pipeline.discover()` at 8, 12, 16, 20 IST.
   `pipeline.gather()` calls each `sources/*.py`, and `sources/base.upsert()` stores new
   postings as `Job(status="new")`.
2. **Filtered.** `pipeline.prefilter_new()` → `prefilter.evaluate()` and `prefilter.too_old()`.
   Survivors become `prefiltered`, the rest `filtered_out` with a `filter_reason`.
3. **Rated and scored.** `scoring.score_pending()` first calls
   `companies.screen_prefiltered()` (AmbitionBox; rated 1 → `filtered_out`), then sends batches
   of 8 to `llm.structured()` with `kb.candidate_brief()`. `scoring.route()` sets `queued_auto`
   (and creates the `Application`), `review` or `skipped`.
4. **Applied.** Every 20 minutes `engine.run_queue()` picks up to 3 queued applications
   (`_next_batch`: resumed ones first). `engine.apply_one()` checks age and blocked sites, then
   `agent.run()` starts Claude with Playwright MCP + `tools.build_server(ctx)`, behind
   `agent.make_guard()`. The agent ends with `report_result` or `report_blocker`; `apply_one`
   turns that into the application's status, an `Event`, and a `notify.send()`.
5. **Stuck.** A blocker with a question → `needs_user`, tab parked in `agent.PARKED`, Telegram
   message from `engine.apply_one`. Your answer → `telegram_bot.answer()` →
   `engine.resume_blocked(id, answer)` (saves to KB, re-queues) → `engine.run_when_free()`.
6. **Replies and status.** Every 15 minutes `tracking.poll()` reads allowlisted Gmail
   (`mail.search_job_mail()`), classifies it, moves the application on, and for a shortlist or
   interview calls `prep.ensure_brief()`. Each morning `tracking.learn_senders()` learns the
   email domains of companies you applied to (`mail.learn_company_domains()`, headers only) and
   reads their job mail. Each night `portal_status.check()` reads Naukri's history API and
   LinkedIn's tracker in the shared browser and `record()`s any status that moved forward.

## Follow a click

| You do | Code path |
|---|---|
| Open a dashboard page | `web/app.py` route → `templates/<page>.html` (extends `base.html`; lists use `_pager.html`) → `static/app.css` |
| Tap a Telegram button | `telegram_bot.run_forever()` → `_handle_update()` → `handle_action("skip:12")` → `engine.*` |
| Type to the chat | `static/chat.js` → `POST /assistant/chat` → `assistant.reply()` (Claude with read tools and `propose_*`) → you tap Confirm → `POST /assistant/confirm/{id}` → `assistant.execute()` |
| Hermes asks something | `web/api.py` (bearer token via `app.api_auth`) |
| `/pause 2h` | `telegram_bot.handle_command()` → `pause.pause_now()`; `scheduler._safe()` and `engine.run_queue()` check `pause.is_paused()` |

## Where to change what

| To change | Edit |
|---|---|
| Roles, locations, keywords, thresholds, caps, schedules, models | `config/settings.yaml`, created from `settings.example.yaml` by `scripts/setup.sh` (the dashboard's overrides live in `data/overrides.yaml` and win) |
| Your facts for forms (CTC, notice, preferences) | Dashboard → Knowledge → Profile (`data/profile.yaml` on the server) |
| What the agent may claim | `data/kb/claims-policy.md` (on the server) |
| How Claude scores, fills forms, reads mail, writes briefs | `.claude/skills/<skill>/SKILL.md` |
| Portal-specific apply hints | `apply/agent.py` → `PORTAL_NOTES` |
| What the agent is never allowed to do | `apply/agent.py` → `make_guard()`, `is_internal()`, `PORTAL_OFF_LIMITS`; tests in `tests/test_agent_guard.py` |
| Which sites an OTP / saved login may be used for | `apply/tools.py` → `RunContext.site()`, `job_domains()`, `own_url()` |
| Which mail may be read | `mail.py` → `trusted_senders()` (platforms, ATS: read as is) vs `allowlist()` (adds company domains, read only when `JOB_SUBJECT` matches), `NEVER_AUTO`, `ReadOnlyIMAP` |
| Dashboard login lockout, same-origin check, prep CSP | `web/app.py` → `login()`, `same_origin_only()`, prep route |
| How portal wording maps to a status | `portal_status.py` → `RULES` |
| A new job source | New `sources/<name>.py` returning `Posting`s; call it from `pipeline.gather()` |
| A new table | A model in `db.py`. New tables are created on startup; there are no migrations, so a new column on an existing table needs a manual `ALTER TABLE` on the server. |
| A dashboard page | Route in `web/app.py` + a template that extends `base.html` |
| A Hermes command | Route in `web/api.py` + `hermes/skill/jobhelper/SKILL.md` |
| A Telegram command | `telegram_bot.handle_command()`, `HELP` and `COMMANDS` |
| The look | `static/app.css` (tokens at the top: Apple system colours, light and dark) |

## Conventions

- **Settings are read on every call** (`config.settings()`), so edits apply without a restart.
  `config.set_override()` writes a dashboard change to `data/overrides.yaml`.
- **Database:** `with db.session() as s:` commits on exit. Anything worth showing on a
  timeline goes through `db.add_event()`.
- **Times** are stored in UTC and shown in IST with the `|ist` template filter.
- **Every Claude call** goes through `usage.allowed(task)` first; it pauses work as your
  subscription's limit windows fill up.
- **Imports inside a function** either break an import cycle (with a comment saying so) or, in
  `cli.py` and `verify.py`, load only what one command needs.
- **Safe defaults:** `dry_run` defaults to on wherever it's read.
- **Shortcuts with a known limit** carry a `shortcut:` comment.

## Tests

```bash
.venv/Scripts/python -m pytest -q        # all of them, about 10 s, no network
```

| File | Covers |
|---|---|
| `test_agent_guard.py` | What the apply agent may and may not do |
| `test_mail_guardrails.py` | Read-only Gmail, the allowlist, subject-gated company mail, learning company domains |
| `test_caps_and_routing.py` | Daily caps, score routing, company-rating routing, job age |
| `test_review_hold.py` | The apply queue: review hold, skip, restart, resume, answers |
| `test_parsers_and_signing.py` | Source parsers, prefilter, email alerts, tracking, Hermes signature |
| `test_companies.py` | AmbitionBox rating, name matching, screening |
| `test_portal_status.py` | Naukri / LinkedIn wording → status, forward-only updates |
| `test_assistant.py` | The chat can only propose; confirm runs it once |
| `test_telegram_bot.py` | Pairing, buttons, answers by button or reply |
| `test_pause.py`, `test_usage.py`, `test_reports.py`, `test_kb_privacy.py` | Pause, usage governor, reports and pages, private KB files |

## On the server

| What | Where |
|---|---|
| Deploy | `scripts/deploy.sh` from this machine |
| Code | `/opt/stacks/jobhelper` (container `jobhelper`) |
| Data | `/opt/stacks/jobhelper/data/`: `jobhelper.sqlite`, `profile.yaml`, `overrides.yaml`, `kb/`, `resumes/`, `artifacts/app-<id>/` (agent transcript and screenshots), `browser/` (Chromium profile) |
| Logs | `docker logs jobhelper` (never paste lines containing `api.telegram.org`: they carry the bot token) |
| Run Python against live data | `docker exec -e PYTHONPATH=/app -w /app jobhelper python -c "..."` |
| Why did application #N do that? | Its timeline on the dashboard, then `artifacts/app-N/transcript.jsonl` |
