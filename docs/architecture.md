# JobHelper architecture and design

JobHelper finds jobs, scores them against the candidate's knowledge base, applies to the best
ones through an AI agent driving a real browser, tracks replies in Gmail, and keeps the user in
the loop through a dashboard and a Telegram bot. It runs as one Docker container on the home
server `your-server` and uses the user's Claude subscription.

## 1. System architecture

Everything JobHelper runs is inside one container. The user reaches it over the tailnet through
Caddy; the outside services it talks to are on the right.

```mermaid
flowchart LR
  subgraph User["User devices (tailnet)"]
    Phone["Phone / laptop browser"]
    TG["Telegram app"]
  end

  subgraph Host["Home server your-server"]
    Caddy["Caddy<br/>jobs.example.com (TLS)"]
    Hermes["Hermes<br/>(Telegram fallback)"]
    subgraph Box["Docker container: jobhelper (172.30.95.0/24)"]
      direction TB
      subgraph Sup["supervisord"]
        App["python -m jobhelper serve<br/>FastAPI dashboard + API<br/>APScheduler jobs<br/>Telegram long-poll"]
        Xvfb["Xvfb :99"]
        VNC["x11vnc :5911"]
        NoVNC["noVNC / websockify :6080"]
      end
      Chrome["Chromium (persistent profile)<br/>CDP :9333"]
      MCP["Playwright MCP<br/>(per apply run)"]
      CLI["Claude Code CLI<br/>(Agent SDK, per call)"]
      Data[("data/ volume<br/>SQLite jobhelper.sqlite<br/>kb/ resumes/ profile.yaml<br/>artifacts/ overrides.yaml")]
    end
  end

  subgraph Ext["Outside services"]
    Anthropic["Claude (subscription)"]
    Boards["Naukri · LinkedIn<br/>Greenhouse · Lever · Ashby"]
    Career["Company career sites"]
    Gmail["Gmail IMAP<br/>(read-only)"]
    TGAPI["Telegram Bot API"]
    AB["AmbitionBox<br/>(company reviews)"]
  end

  Phone -->|HTTPS| Caddy -->|127.0.0.1:8095| App
  Caddy -->|/vnc| NoVNC --> VNC --> Xvfb
  Chrome --- Xvfb
  App -->|CDP| Chrome
  App -->|spawns| CLI -->|spawns| MCP -->|CDP| Chrome
  CLI --> Anthropic
  App --> Data
  App -->|HTTP APIs / browser| Boards
  Chrome --> Career
  App -->|IMAP SSL| Gmail
  App <-->|long-poll + send| TGAPI <--> TG
  App -.->|fallback webhook| Hermes -.-> TGAPI
  App -->|HTTP| AB
```

Key points:
- **One process does the work** (`app`): web server, scheduler and Telegram bot share one asyncio loop.
- **One shared browser.** Discovery, the apply agent and the user (through noVNC) all use the same
  logged-in Chromium. Each application gets its own tab; a stuck one keeps its tab open.
- **Claude runs through the Agent SDK**, which starts the Claude Code CLI with the subscription token.
  Scoring and email triage are one-shot calls with no tools; the apply agent gets the browser
  (Playwright MCP) and JobHelper's own tools, behind a guard hook.
- **Gmail is read-only and allowlisted**: only job platforms and companies applied to.

## 2. Job pipeline

```mermaid
flowchart TD
  subgraph Discover["Discover (08:00, 12:00, 16:00, 20:00 IST)"]
    S1["ATS boards<br/>(Greenhouse, Lever, Ashby)"]
    S2["LinkedIn guest API"]
    S3["Naukri (browser)"]
    S4["Job-alert emails"]
  end
  S1 & S2 & S3 & S4 --> Up["upsert into jobs<br/>status = new"]
  Up --> Pre{"prefilter<br/>title · location · experience · age ≤ 7d"}
  Pre -->|fail| FO["filtered_out"]
  Pre -->|pass| PF["prefiltered"]
  PF --> CR{"company rating<br/>(AmbitionBox)"}
  CR -->|rated 1| FO
  CR -->|5 / 3 / unrated| Score["Claude scoring (Haiku)<br/>batches of 8, 0-100"]
  Score --> Route{"route"}
  Route -->|"≥ 85, or ≥ 80 if company rated 5<br/>no red flags, location ok, under cap"| Q["queued_auto<br/>+ application queued"]
  Route -->|"60 – 84"| Rev["review"]
  Route -->|"< 60"| Sk["skipped"]
  Rev -->|user approves| Ap["approved<br/>+ application queued"]
  Rev -->|user skips| Dis["dismissed"]
  Q & Ap --> Apply["apply queue<br/>every 20 min, 09:00-21:40 IST"]
```

## 3. Application lifecycle

```mermaid
stateDiagram-v2
  [*] --> queued
  queued --> not_eligible: posted over 7 days ago
  queued --> apply_yourself: site blocks automation
  queued --> applying: apply run picks it
  applying --> applied: agent submitted
  applying --> already_applied
  applying --> not_eligible
  applying --> needs_user: question / OTP / blocker
  applying --> awaiting_review: dry run, form filled
  applying --> failed
  applying --> queued: Claude limit hit (deferred)
  needs_user --> queued: user answers or taps Resume<br/>(front of queue, same tab)
  awaiting_review --> applied: "I submitted it"
  awaiting_review --> review_expired: after 12 h
  apply_yourself --> applied: "I submitted it"
  failed --> queued: Restart
  needs_user --> skipped_by_user: Skip
  queued --> skipped_by_user: Skip
  applied --> viewed: Gmail or portal tracking
  viewed --> shortlisted
  shortlisted --> assessment
  shortlisted --> interview
  assessment --> interview
  interview --> offer
  applied --> rejected
  interview --> rejected
```

## 4. A stuck application, end to end

```mermaid
sequenceDiagram
  autonumber
  participant Sch as Scheduler
  participant Eng as apply.engine
  participant Ag as apply.agent (Claude + browser)
  participant Br as Chromium tab
  participant TG as Telegram bot
  participant U as User
  participant KB as Knowledge base

  Sch->>Eng: run_queue() (up to 3 applications)
  Eng->>Ag: run(ctx) for application #80
  Ag->>Br: browser_tabs(new) and fill the form
  Ag-->>Eng: report_blocker(question, options)
  Eng->>Eng: status = needs_user, tab parked
  Eng->>TG: ❓ "#80 ... asks: Willing to work in shifts?" [Yes] [No]
  Eng->>Ag: next application, in a new tab
  U->>TG: taps "Yes" (or replies with text)
  TG->>Eng: resume_blocked(80, "Yes")
  Eng->>KB: add_note(question, answer)
  Eng->>Eng: status = queued (first in line)
  Eng->>Ag: run(ctx, resumed_from = question + answer + page URL)
  Ag->>Br: browser_tabs(select) the parked tab, enter "Yes", submit
  Ag-->>Eng: report_result(applied)
  Eng->>TG: ✅ applied
```

## 5. Module design

Each box is a Python module (or class) with its main public functions. Arrows point from a module
to the modules it calls.

```mermaid
classDiagram
  direction LR

  class web_app["web.app (FastAPI)"] {
    +lifespan()
    +page routes: home, review, jobs, applications, kb, settings
    +answer_page() resume_application_page() skip_page() restart_page()
    +assistant_chat() assistant_confirm()
  }
  class scheduler {
    +build() AsyncIOScheduler
    discover · apply · email · summary
    catch-up scoring · prep retry · pause watch · weekly
  }
  class web_api["web.api (APIRouter)"] {
    +/api/* routes for Hermes (bearer token)
  }
  class cli {
    +main() serve | discover | apply | verify | doctor
  }
  class telegram_bot {
    +run_forever()
    +send(text, buttons)
    +handle_action(data) handle_command(text)
    +answer(app_id, text)
    +question_buttons() application_buttons()
  }
  class assistant {
    +reply(messages)
    +propose_change(changes) propose_action(action)
    +execute(proposal_id)
    +preferences() status() list_jobs() get_application()
  }
  class pipeline {
    +discover(sources)
    +gather() enrich()
    +prefilter_new()
  }
  class prefilter {
    +evaluate() check_title() check_location()
    +check_experience() too_old()
  }
  class companies {
    +screen_prefiltered()
    +ensure(names) fetch(name)
    +grade(ratings) band() pick() describe()
  }
  class scoring {
    +score_pending(limit)
    +route(session, job) str
    +system_prompt()
  }
  class engine["apply.engine"] {
    +run_queue(max_runs, only)
    +apply_one(application_id)
    +approve() dismiss() mark_submitted()
    +resume_blocked(id, answer) run_when_free(id)
    +restart_application() remove_application()
    +last_block() recover_stranded()
  }
  class agent["apply.agent"] {
    +run(ctx, portal, start_url)
    +make_guard(ctx) PreToolUse hook
    +system_prompt(ctx, portal)
    +preflight() release_tabs()
    PARKED: app_id → tabs
  }
  class tools["apply.tools"] {
    RunContext
    +build_server(ctx)
    get_job · get_profile · kb_search · get_resume
    get_site_login · create_site_login · fetch_email_code
    record_answer · report_blocker · report_result
  }
  class vault["apply.vault"] {
    +get(domain) create(domain, email) list_logins()
  }
  class tracking {
    +poll(days)
    +is_job_alert(mail)
  }
  class prep {
    +ensure_brief(app_id, trigger) retry_pending()
  }
  class portal_status {
    +check() daily 22:15 IST
    +status_of(text) naukri_statuses() linkedin_statuses()
    +record(source, found)
  }
  class summary {
    +stats() daily_text() period_text(days)
    +send_daily() send_weekly()
  }
  class llm {
    +structured(prompt, schema, model, task)
    +base_env() check()
  }
  class usage {
    +allowed(task) observe(msg) record(result)
    +note_limit_hit() today()
  }
  class mail {
    +search(senders, query) search_job_mail()
    +latest_verification(site_domain)
    +allowlist() ReadOnlyIMAP
  }
  class kb {
    +search(q) reindex() add_note()
    +candidate_brief() claims_policy()
  }
  class notify {
    +send(text, kind, buttons) link(path)
  }
  class pause {
    +pause_now(text) resume_now() current() is_paused()
  }
  class browser {
    SharedBrowser: context() page() cdp_url
  }
  class config {
    +settings() set_override() profile()
    +secrets() profile_for_forms()
  }
  class db {
    +session() engine() add_event() run_log()
    ORM models (see section 6)
  }
  class sources {
    ats · linkedin · naukri · indeed · email_alerts
    base.Posting · base.upsert()
  }

  web_app --> engine
  web_app --> web_api
  web_api --> engine
  web_app --> assistant
  web_app --> telegram_bot
  web_app --> scheduler
  scheduler --> pipeline
  scheduler --> engine
  scheduler --> tracking
  scheduler --> summary
  scheduler --> scoring
  scheduler --> prep
  scheduler --> portal_status
  portal_status --> browser
  pipeline --> sources
  pipeline --> prefilter
  pipeline --> scoring
  scoring --> companies
  scoring --> llm
  scoring --> kb
  engine --> agent
  engine --> notify
  engine --> kb
  agent --> tools
  agent --> browser
  agent --> llm
  tools --> vault
  tools --> mail
  tools --> kb
  tracking --> mail
  tracking --> llm
  tracking --> prep
  tracking --> notify
  prep --> llm
  assistant --> engine
  assistant --> summary
  notify --> telegram_bot
  telegram_bot --> engine
  telegram_bot --> pause
  web_app --> pause
  llm --> usage
  sources --> browser
```

Everything also uses `config` (settings, profile, secrets) and `db` (SQLite); those arrows are left
out to keep the picture readable.

## 6. Data model

```mermaid
erDiagram
  JOBS ||--o{ APPLICATIONS : "applied through"
  APPLICATIONS ||--o{ EVENTS : "timeline"
  JOBS ||--o{ EVENTS : "timeline"
  COMPANY_RATINGS ||..o{ JOBS : "matched by normalised company name"

  JOBS {
    int id PK
    string source "naukri, linkedin, greenhouse ..."
    string external_id "unique with source"
    string title
    string company
    string status "new, prefiltered, review, queued_auto ..."
    float score "0-100"
    json score_detail "reasons, gaps, red_flags"
    datetime posted_at
    datetime discovered_at
  }
  APPLICATIONS {
    int id PK
    int job_id FK
    string portal "daily cap per portal"
    string mode "auto, approved"
    string status "queued, applying, applied, needs_user ..."
    int attempts
    text blocker
    json answers "question, answer, source"
    datetime applied_at
  }
  EVENTS {
    int id PK
    int application_id FK
    int job_id FK
    string kind "apply_applied, apply_needs_user, resumed ..."
    text summary
    json detail
    datetime at
  }
  COMPANY_RATINGS {
    string name PK
    int overall "1, 3, 5 or null"
    json detail "per-criterion scores, url"
    datetime fetched_at
  }
  EMAILS_SEEN {
    string message_id PK
    string kind "alert, tracking, ignored"
    json result
  }
  SITE_LOGINS {
    string domain PK
    string username
    text password_enc "Fernet"
  }
  LLM_USAGE {
    int id PK
    string task
    string model
    int input_tokens
    float cost_usd
  }
  RATE_LIMITS {
    string limit_type PK
    float utilization
    datetime resets_at
  }
  RUNS {
    int id PK
    string kind
    json stats
    bool ok
  }
```

Full column list: [database-schema.md](database-schema.md).

## 7. Guardrails and where they live

| Guardrail | Enforced in |
|---|---|
| Gmail is read-only; only allowlisted senders are read; never reply or delete | `mail.ReadOnlyIMAP`, `mail.allowlist()`, `mail.NEVER_AUTO` |
| Platform / ATS mail is trusted; company mail is opened only when the subject is about a job | `mail.trusted_senders()`, `mail.JOB_SUBJECT` |
| Apply agent: no webmail, no Google/Apple/Microsoft sign-in, only the chosen resume uploaded, own tab only, no closing tabs | `apply.agent.make_guard` (PreToolUse hook, holds whatever the model decides) |
| Apply agent: no internal/private addresses or non-web URLs; no portal messaging or profile pages | `apply.agent.is_internal`, `apply.agent.PORTAL_OFF_LIMITS` |
| OTPs and saved logins only for this job's sites; passwords masked in logs | `apply.tools.RunContext.site()` / `own_url()`, transcript masking in `apply.agent` |
| Secrets never reach the browser or the Claude CLI (except the Claude token) | `browser.child_env()`, `llm.base_env()` |
| Dashboard: same-origin form posts, login lockout, strict CSP on prep pages | `web.app.same_origin_only`, `web.app.login`, prep route |
| Agent has no file or shell tools; only the browser + JobHelper tools | `apply.agent.run` (`tools=[]`, allowlist) |
| Claims policy: no invented numbers, no salary ranges, no personal projects in applications | `data/kb/claims-policy.md`, injected into every prompt |
| Claude usage: background work pauses at 60%, applying at 75%, email at 95%; max apply runs per day | `usage.allowed` (checked inside the LLM semaphore) |
| Chat assistant can only propose; nothing changes until the user taps Confirm | `assistant.propose_*` / `assistant.execute` |
| Telegram bot answers only the paired chat | `telegram_bot._handle_update` |
| Daily cap per portal, 7-day job age, company rated 1 dropped | `scoring.route`, `prefilter.too_old`, `companies.screen_prefiltered` |
