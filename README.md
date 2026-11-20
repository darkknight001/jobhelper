# JobHelper

A self-hosted job-search assistant for job seekers in India. You run your own copy, on your own always-on machine, with your own Claude subscription. It:

- finds jobs on Naukri, LinkedIn, company ATS boards and job-alert emails,
- drops companies with poor employee reviews and scores the rest against your resume and notes,
- applies to the strongest matches (daily cap per portal) and queues the rest for your review,
- tracks recruiter replies in Gmail and writes prep briefs when you are shortlisted,
- reports to you through your own Telegram bot.

## Quick start

```bash
git clone https://github.com/darkknight001/jobhelper.git && cd jobhelper
scripts/setup.sh                  # creates deploy/.env, config/settings.yaml, data/
# edit deploy/.env: Claude token, Gmail app password, Telegram bot token, dashboard password
docker compose up -d --build      # then open http://localhost:8095
```

Full walk-through (about 45 minutes, every step with what success looks like): **[docs/SETUP.md](docs/SETUP.md)**.

## Documentation

- Setup guide: [docs/SETUP.md](docs/SETUP.md)
- Decisions and why: [docs/DESIGN.md](docs/DESIGN.md)
- How it works, with diagrams: [docs/architecture.md](docs/architecture.md)
- Finding your way around the code: [docs/CODEBASE.md](docs/CODEBASE.md)
- Tables and columns: [docs/database-schema.md](docs/database-schema.md)

## Where things are

| Path | What |
|---|---|
| `config/settings.yaml` | Your settings: targets, keywords, boards, thresholds, caps, schedules, models. Copied from `config/settings.example.yaml` by `scripts/setup.sh` |
| `data/profile.yaml` | Your structured facts for forms. Empty fields mean "ask me". |
| `data/kb/claims-policy.md` | Rules for what the agent may say about you |
| `data/kb/*.md` | Knowledge base (resume, claude.ai export, notes) |
| `data/resumes/*.pdf` | Resume variants; the scorer picks one per job |
| `.claude/skills/` | The four skills; the harness uses them as its prompts |
| `.claude/agents/harness-verifier.md` | Verifier subagent |
| `hermes/` | Optional Hermes skill + webhook route (not needed) |
| `deploy/`, `compose.yaml` | Container, supervisor, Caddy site |

## Daily use

- **Telegram bot:** instant alerts for interviews, assessments, recruiter questions, offers and
  blockers, with tap buttons, plus a 21:00 IST summary. Commands: `/status`, `/pending`,
  `/review`, `/pause 2h`, `/resume`. When an application asks something only you know, tap an
  answer or reply to the message; it's saved to your knowledge base.
- **Dashboard** (`http://localhost:8095`, or your own address): review queue, applications and their
  timelines, prep briefs, KB/profile editing, settings, the remote browser, and a chat
  assistant (bottom right) that proposes changes you confirm.
- **Status tracking:** from Gmail every 15 minutes, from Naukri's application history and
  LinkedIn's job tracker every night at 22:15 IST, and from companies' own email domains
  (learned each morning). Shortlists alert you on Telegram with a prep brief.
- **Dry run is ON by default.** Watch a few runs in the remote browser, check the answers
  on each application page, then switch it off in Settings. Dry run is best-effort: the guard
  blocks known submit buttons, Enter and line breaks, but the final check is you.

## Guardrails (enforced in code)

- **Gmail:** read-only IMAP (EXAMINE + BODY.PEEK). Every command that could modify mail is
  refused. Searches are limited to job platforms, ATS domains and companies you applied to,
  and the sender is re-checked before a message is opened. A company's mail is opened only when
  its subject is about a job application, so a company you're also a customer of never has its
  other mail read. No code sends email.
  Verification codes are read only from the career site being applied to.
- **Recruiter mail:** interviews, assessments and questions are reported to you, never
  answered or accepted. A suggested reply appears as a draft on the dashboard for you to send.
- **Apply agent:** no shell or file tools; only an allowlist of browser tools (no JavaScript
  execution, cookies or storage); webmail blocked; uploads only from `data/resumes/`; stops on
  CAPTCHAs, phone OTPs and any question whose answer isn't in your profile or KB.
  It can't open internal or private addresses (localhost, LAN, cloud metadata) or non-web URLs
  (`file:`, `javascript:`), and messaging, feed, settings and profile-edit pages on LinkedIn and
  Naukri are off-limits. OTPs and saved logins are handed out only for the sites of the job it's
  working on, and site passwords are masked in the saved transcript.
- **Web dashboard:** form posts must come from the dashboard's own origin, repeated wrong
  passwords lock the client out for a while, and interview-prep pages are served with a strict
  Content-Security-Policy (no scripts).
- **Secrets:** the browser gets none of them and the Claude CLI gets only the Claude token; the
  Gmail password, vault key and the rest never reach either.
- **Caps:** a daily cap per portal, approved jobs included. Auto-apply needs a score ≥ 85
  (80 for a company rated 5) with no red flags; nothing older than 7 days.

The app password itself can do more than this; the code is what limits it.

## Commands

```bash
python -m jobhelper verify          # test suite + config/DB audits (add --llm for model evals)
python -m jobhelper doctor          # Claude auth, Gmail, Hermes, npx
python -m jobhelper discover        # find + score now
python -m jobhelper apply --max 1   # run the apply queue now
python -m jobhelper summary         # print today's summary (--send to Telegram)
python -m jobhelper kb-import export.md
```

Inside Docker, prefix with `docker exec jobhelper`, e.g. `docker exec jobhelper python -m jobhelper doctor`.

## Update and deploy

On the machine that runs it: `git pull && docker compose up -d --build`.

From another machine over SSH:

```bash
JOBHELPER_HOST=your-server scripts/deploy.sh
```

It stops if a local test fails or if `config/settings.yaml` differs from the server's copy
(re-run with `--force` once you have checked the diff), then copies the code, rebuilds the
container and runs the tests there. `data/` and `deploy/.env` stay on the server. Details and
backups: [docs/SETUP.md](docs/SETUP.md).

## Skills elsewhere

- **Claude Code:** the skills in `.claude/skills/` load automatically in this repo. For all
  projects, copy them to `~/.claude/skills/`.
- **claude.ai:** run `python scripts/package_skills.py` and upload each zip from `dist/skills/`
  under Settings → Capabilities → Skills. Attach or paste your KB in the chat, since the
  skills only read `data/` inside this repo.

## Verifier

In Claude Code: "use the harness-verifier agent to evaluate JobHelper". It runs `verify`,
reviews guardrail code and agent transcripts, and reports PASS/FAIL with fixes. It never
edits anything.

## Disclaimer

Naukri, LinkedIn and Indeed prohibit automated applying in their terms, and automation can get
an account restricted. Applications are sent on your behalf and answers can be wrong. Keep dry
run on until you trust it, review what it submits, and use JobHelper at your own risk. It is
provided as is, with no warranty.
