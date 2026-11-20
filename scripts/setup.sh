#!/usr/bin/env bash
# First-time setup. Run from the repo root. Safe to re-run: never overwrites existing files.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV=deploy/.env
if [ ! -f .env.example ]; then echo "run from the JobHelper repo (.env.example missing)" >&2; exit 1; fi

if [ ! -f "$ENV" ]; then
  mkdir -p deploy
  (umask 077; cp .env.example "$ENV")
  python3 - "$ENV" <<'PY'
import base64, os, re, secrets, sys
p = sys.argv[1]
gen = {
    "JOBHELPER_SESSION_SECRET": lambda: secrets.token_urlsafe(32),
    "JOBHELPER_API_TOKEN": lambda: secrets.token_urlsafe(32),
    "JOBHELPER_VAULT_KEY": lambda: base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),  # Fernet key
    "JOBHELPER_VNC_PASSWORD": lambda: secrets.token_urlsafe(9),
    "HERMES_WEBHOOK_SECRET": lambda: secrets.token_urlsafe(32),
}
todo = {  # you must obtain these
    "CLAUDE_CODE_OAUTH_TOKEN": "CHANGE_ME_run_claude_setup-token",
    "JOBHELPER_DASHBOARD_PASSWORD": "CHANGE_ME_choose_a_strong_password",
    "GMAIL_ADDRESS": "CHANGE_ME_you@gmail.com",
    "GMAIL_APP_PASSWORD": "CHANGE_ME_google_app_password",
    "JOBHELPER_PUBLIC_URL": "http://localhost:8095",
    "TELEGRAM_BOT_TOKEN": "CHANGE_ME_from_BotFather",
}
lines = open(p, encoding="utf-8").read().splitlines()
seen, out = set(), []
for ln in lines:
    m = re.match(r"^([A-Z0-9_]+)=(.*)$", ln)
    if m and m[1] in gen:
        ln = f"{m[1]}={gen[m[1]]()}"; seen.add(m[1])
    elif m and m[1] in todo:
        ln = f"{m[1]}={todo[m[1]]}"; seen.add(m[1])
    out.append(ln)
for k in ("JOBHELPER_VNC_PASSWORD",):  # generated secrets not present in the template
    if k not in seen:
        out.append(f"{k}={gen[k]()}")
open(p, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
  chmod 600 "$ENV"
  echo "created $ENV (generated secrets filled; CHANGE_ME placeholders need your values)"
else
  echo "$ENV exists, left untouched"
fi

mkdir -p data/kb data/resumes data/artifacts data/browser
copy_if_missing() { [ -f "$2" ] && echo "$2 exists, kept" || { cp "$1" "$2"; echo "created $2"; }; }
copy_if_missing config/profile.example.yaml data/profile.yaml
copy_if_missing config/claims-policy.example.md data/kb/claims-policy.md
copy_if_missing config/settings.example.yaml config/settings.yaml

cat <<'TXT'

Next steps (details: docs/SETUP.md)
 1. Edit deploy/.env and replace every CHANGE_ME value.
 2. docker compose up -d --build
 3. Open the dashboard (JOBHELPER_PUBLIC_URL) and log in with your dashboard password.
 4. Upload your resume, then fill data/profile.yaml (or the Profile page) and edit data/kb/claims-policy.md.
 5. Pair Telegram with your bot.
 6. docker exec jobhelper python -m jobhelper doctor
TXT
