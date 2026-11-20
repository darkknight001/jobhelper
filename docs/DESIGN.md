# JobHelper — design

A single-user harness that finds jobs that fit the candidate's profile, applies to the strongest
matches automatically, queues the rest for review, tracks replies from Gmail, and reports
through its own Telegram bot. Runs on the home server **your-server**.

This file keeps the decisions and their reasons. How it works, with diagrams:
[architecture.md](architecture.md). Where to find things in the code: [CODEBASE.md](CODEBASE.md).

## Decisions (from the kickoff, 2026-11-09)

| Topic | Decision |
|---|---|
| Where | Docker on your-server, next to Hermes. Dashboard at `https://jobs.example.com` (LAN/tailnet only, like dockge), mobile first. |
| LLM | Your **Claude subscription** through the Claude Agent SDK, authenticated with `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`. No API credits. Single-user only. |
| Auto-apply | Score ≥ `auto_apply_min` (85; 80 when the company is rated 5) **and** under the per-portal daily cap (15, set on the dashboard). Everything between `review_min` (60) and that goes to review; approved jobs are applied by the same engine. Postings older than 7 days are never applied to. |
| Sources | Naukri, LinkedIn, Greenhouse / Lever / Ashby boards, company career sites that portals redirect to, and job-alert emails (Naukri Priority Applicant, LinkedIn, Indeed). Indeed's own search is off: Cloudflare blocks the remote browser there (2026-11-10). |
| Email | Gmail over IMAP with an app password, read-only. |
| Alerts | JobHelper's own Telegram bot (your bot) with tap buttons; Hermes' `deliver_only` webhook route is the fallback. No LLM tokens for alerts. |
| Company quality | AmbitionBox reviews, rated 1 / 3 / 5 on culture, benefits, type of work and work-life. Rated 1 is dropped before scoring. |
| KB | Markdown files in `data/kb/` plus a SQLite FTS index. Seeded from the resume; your claude.ai export goes in via the dashboard. |

## Architecture

Diagrams and the module map are in [architecture.md](architecture.md).

### One browser, two drivers

A single headed Chromium runs inside Xvfb with a persistent profile (`data/browser/`), so
portal logins survive restarts. Discovery code drives it through Playwright; the apply agent
drives the **same** browser through `@playwright/mcp --cdp-endpoint`. You log in to
Naukri, LinkedIn and Indeed once through noVNC (`/vnc/` on the dashboard), and solve any
CAPTCHA the same way from your phone. An asyncio lock keeps one driver at a time. Each
application works in its own tab; one that needs you keeps its tab open while the next job
starts in a new one.

### Discovery

| Source | How |
|---|---|
| Greenhouse / Lever / Ashby | Public JSON board APIs for a target-company list in `config/settings.yaml`. Lowest risk. |
| LinkedIn | Public guest search endpoint (no login) for discovery; Easy Apply through the logged-in browser. |
| Naukri | Search API called **from inside the logged-in page** (real cookies and headers). |
| Indeed | Search page in the browser (switched off: set `search.locations.indeed` to turn it back on). |
| Email alerts | IMAP search for alert senders; job URLs extracted and resolved to postings. Priority Applicant alerts go first, because applying early is the point of that subscription. |

Every posting is normalised into one `Job` row with a stable `(source, external_id)` key, so a
job seen in an alert email and in search is stored once.

### Scoring

1. **Prefilter (free):** title must match a target-role pattern; drop excluded stacks and
   levels (intern, QA-only, frontend-only, Java/.NET-only, sales); location must be Delhi NCR,
   Bangalore or remote India; the experience band must overlap ~6 years.
2. **LLM (batched):** up to 8 jobs per call against a compact candidate brief built from the
   KB. Returns `score`, `reasons`, `gaps`, `red_flags`, `resume_variant`. Structured output via
   JSON schema.
3. **Company rating (free, cached 30 days):** AmbitionBox ratings → 1 / 3 / 5. Rated 1 is
   filtered out before the LLM sees it (`companies.py`).
4. **Routing:** `score ≥ auto_apply_min` (or ≥ 80 for a company rated 5) → `queued_auto` (if the
   portal's cap allows, otherwise review); `review_min ≤ score` → `review`; below → `skipped`.

### Applying

One agentic engine for every portal, with a per-portal preamble (where the Apply button is,
what success looks like, known questionnaire quirks). The agent gets:

- Playwright MCP browser tools on the shared Chromium.
- In-process tools: `kb_search`, `get_profile`, `get_resume`, `get_site_login` /
  `create_site_login` (encrypted vault, unique password per site), `fetch_email_code`
  (verification codes and links from Gmail), `report_blocker`, `report_result`.
- Hard rails (PreToolUse hooks): no JavaScript execution, file uploads only from
  `data/resumes/`, and in **dry-run** mode final-submit clicks are denied.
- Also: no Google / Apple / Microsoft sign-in, no webmail, and the agent works only in its own tab.
- No internal or private addresses and no non-web URLs (`agent.is_internal`); messaging, feed,
  settings and profile-edit pages on LinkedIn / Naukri are off-limits (`agent.PORTAL_OFF_LIMITS`).
- Scoped per run (`tools.RunContext`): OTP lookups and saved logins work only for `site()`s of this
  job (its URLs, pages it opened, logins it created); URLs it reports are clamped by `own_url()`.
  Passwords it was given are masked in the transcript.
- A blocker ends the run as `needs_user`. A question only you can answer comes to Telegram (and
  the application page) with option buttons; your answer is saved to the KB and the application
  goes to the front of the queue, continuing in its parked tab. For an OTP or CAPTCHA, finish it
  in noVNC and tap **Resume**.

Caps are counted per portal per IST day from `applications.applied_at`; company sites count
against the portal that sent you there.

### Tracking replies and status

Three sources, because no single one sees everything. A status only ever moves forward.

1. **Gmail, every 15 minutes** (`tracking.poll`). Messages from portals, ATS systems or
   companies you applied to are classified (received / viewed / shortlisted / assessment /
   interview / rejected / offer / recruiter outreach), matched to an application, and appended
   to its timeline. Shortlists, interviews and offers alert you immediately.
2. **Company email domains, daily at 07:10 IST** (`tracking.learn_senders`). Many applications
   go from a portal to the company's own site (55 of the first 77 from LinkedIn did), and the
   company then writes from its own domain. JobHelper reads only the From and Subject headers of
   mail whose sender matches a company you applied to, learns the domain if it carries the
   company's name and the subject is about a job, then reads that domain's job mail. Company
   mail (career sites, their logins, learned domains) is always subject-gated; job platforms and ATS from settings are trusted, so a company you're also a customer of never has its other mail
   read. The daily summary lists company-site applications with no confirmation after 3 days.
3. **The portals themselves, daily at 22:15 IST** (`portal_status.check`). Naukri's application
   history (its JSON API, read from the logged-in page) and LinkedIn's job tracker (Applied
   cards) show "Application viewed", "Resume viewed", "Shortlisted" or "Not selected" that
   often never arrive by email. Matched by the portal's job id. Read-only: the only clicks are
   LinkedIn's page numbers. "No longer accepting applications" means the posting closed and is
   ignored.

### Notifications and control

- Instant, through the bot: interview/shortlist/offer, recruiter outreach, questions and other
  `needs_user` blockers, failures. Each comes with tap buttons (submitted / skip / resume / restart).
- Daily summary (21:00 IST) and a weekly report.
- Bot commands: `/status`, `/week`, `/pending`, `/review`, `/pause 2h`, `/resume`.
- The dashboard has a chat assistant that can answer questions and *propose* changes; nothing
  changes until you tap Confirm.
- Hermes is the fallback for alerts and can still call the JSON API (`web/api.py`) with a bearer
  token through its skill (`hermes/skill/jobhelper/SKILL.md`).

### Job states

```
new → filtered_out                              (rules, age, or company rated 1)
    → prefiltered → scored → skipped
                           → review → approved → applying → applied → (viewed|shortlisted|interview|offer|rejected)
                                    → dismissed           ↘ needs_user → queued (front of the queue)
                           → queued_auto → applying …     ↘ awaiting_review · apply_yourself · failed
```

## Deployment on your-server

- `compose.yaml` (Dockge stack at `/opt/stacks/jobhelper`). The container has **its own
  network** (172.30.95.0/24), so Chromium's control port (9333) and raw VNC (5911) are
  reachable only inside it. Published on the host's loopback only: dashboard
  `127.0.0.1:8095` and noVNC `127.0.0.1:6080`. noVNC needs a VNC password, which the
  dashboard's link carries in the URL fragment so it never reaches server logs.
- Caddy site `https://jobs.example.com` on 443 only, with DNS pointing at the server's
  Tailscale IP, so it's reachable only on the tailnet. `/vnc/*` goes through `forward_auth`
  to the dashboard's session check. The app runs uvicorn with `ws="none"`, otherwise the
  forwarded WebSocket headers make that check fail with 403.
- Hermes: `platforms.webhook` on `0.0.0.0:8644` with one `deliver_only` route `jobhelper` →
  telegram. ufw admits only JobHelper's subnet; Tailscale admits your own devices before
  ufw. Every request must carry an HMAC signature less than 5 minutes old.
- The agent's browser never holds a Google session. Social sign-in (Google, Apple,
  Microsoft) and account pages are blocked; SSO-only sites go to review.
- Secrets never reach the browser, and the Claude CLI gets only the Claude token (`browser.child_env`,
  `llm.base_env`). The dashboard checks Origin/Referer on form posts, locks out repeated failed
  logins and serves prep pages with a strict CSP.
- Secrets live only in `deploy/.env` on the server: Claude OAuth token, Gmail app password,
  Hermes route secret, dashboard password, API token, vault key, VNC password.

## Risks you should know about

- **Portal terms of service.** Naukri, LinkedIn and Indeed forbid automated applying. The
  caps, human-like pacing, a real headed browser and stopping on CAPTCHAs reduce the risk of
  account restrictions; they don't remove it.
- **Subscription limits.** Each application is a multi-turn agent run. Models are set per
  task in `config/settings.yaml`. Move scoring to Haiku if you hit your usage limits.
- **Wrong answers on forms.** The agent answers only from the KB and profile, and reports
  a blocker rather than guessing on anything legal, salary or identity related. Dry-run mode
  lets you watch several runs before going live.
