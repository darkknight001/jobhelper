# Security

## Reporting a vulnerability

Open a private security advisory on this repository (Security tab, "Report a vulnerability").
Please don't file public issues for security problems, and don't include real credentials or
personal data in a report.

## Threat model

- **Single user, self-hosted.** One person runs it for themselves; there are no accounts or tenants.
- **Never expose it to the internet.** Keep the dashboard on a LAN or tailnet (or behind a VPN and
  the Caddy login check). The login lockout and same-origin check are defence in depth, not a
  public-facing hardening.
- **Prompt injection is expected.** Job posts, career pages and emails are untrusted text that an
  LLM reads. The controls are enforced in code (hooks, allowlists, per-run scoping), not in
  prompts, but they are best-effort and not a proof of safety.
- **Keep dry run on until you trust it.** In dry run the agent can never press a final submit.
- **Secrets stay server-side.** `deploy/.env` and `data/` hold your tokens, mail app password and
  vault; never commit them. The browser gets none, the Claude CLI only its own token.
- **The Gmail app password can do more than JobHelper does.** Only code limits it to read-only job mail.

## Where the guardrails are in code

- Mail: `jobhelper/mail.py` (`ReadOnlyIMAP`, `allowlist()`, `trusted_senders()`, `NEVER_AUTO`)
- Apply agent: `jobhelper/apply/agent.py` (`make_guard`, `is_internal`, `PORTAL_OFF_LIMITS`) and
  `jobhelper/apply/tools.py` (`RunContext.site`)
- Secret scrubbing: `jobhelper/browser.py` (`child_env`), `jobhelper/llm.py` (`base_env`)
- Dashboard: `jobhelper/web/app.py` (`same_origin_only`, `login`, prep page CSP)
- Chat assistant: `jobhelper/assistant.py` (propose, then confirm)

See also the Guardrails section of [README.md](README.md) and the tests in `tests/`.
