"""JobHelper's own Telegram bot: alerts with tap buttons, a few commands, no AI in the loop.

Setup: create a bot with @BotFather, put its token in deploy/.env as TELEGRAM_BOT_TOKEN, then
send `/start <code>` to it, where <code> is the pairing code shown on the dashboard (Settings).
After pairing the bot answers only that one chat and ignores everyone else.

Button taps arrive as callback data "<action>:<id>":
  sub = I submitted it · skip = skip application · rst = restart · res = resume · self = apply yourself
  apr = approve review-queue job · dis = skip review-queue job
  ans = "<app>:<n>": answer the question an application stopped on with its n-th option
A plain-text reply to a "❓ #<app> ... asks:" message answers that question.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
from typing import Any

import httpx
from sqlalchemy import select

from . import config, db, pause, summary

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
STATE = config.DATA_DIR / "telegram.json"
LIMIT = 3800

Buttons = list[list[tuple[str, str]]]   # rows of (label, callback_data)


# ------------------------------------------------------------------ state / pairing

def token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _state() -> dict[str, Any]:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(st: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st), encoding="utf-8")
    try:
        os.chmod(STATE, 0o600)
    except OSError:
        pass


def pairing_code() -> str:
    """One-time code shown on the dashboard; created on first ask, cleared once paired."""
    st = _state()
    if not st.get("pair_code"):
        st["pair_code"] = secrets.token_hex(5).upper()
        _save(st)
    return st["pair_code"]


def paired_chat() -> int | None:
    return _state().get("chat_id")


def status() -> dict[str, Any]:
    if not token():
        return {"state": "no_token"}
    if paired_chat():
        return {"state": "paired", "name": _state().get("name", "")}
    return {"state": "waiting", "code": pairing_code(), "bot": _state().get("bot_username", ""),
            "error": _state().get("last_error", "")}


def active() -> bool:
    return bool(token() and paired_chat())


def unpair() -> None:
    _save({})


# ------------------------------------------------------------------ Bot API

async def _call(method: str, payload: dict[str, Any], timeout: float = 20) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(API.format(token=token(), method=method), json=payload)
            data = r.json()
    except httpx.HTTPError as exc:
        # httpx error text can include the request URL, which carries the bot token.
        raise RuntimeError(f"Telegram {method}: {type(exc).__name__}") from None
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description', r.status_code)}")
    return data["result"]


def _markup(buttons: Buttons | None) -> dict[str, Any] | None:
    if not buttons:
        return None
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in buttons]}


def _chunks(text: str) -> list[str]:
    if len(text) <= LIMIT:
        return [text]
    out, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > LIMIT:
            out.append(buf)
            buf = ""
        buf += line + "\n"
    return out + ([buf] if buf.strip() else [])


async def send(text: str, buttons: Buttons | None = None) -> bool:
    chat = paired_chat()
    if not (token() and chat):
        return False
    parts = _chunks(text)
    for i, part in enumerate(parts):
        payload: dict[str, Any] = {"chat_id": chat, "text": part, "disable_web_page_preview": True}
        if i == len(parts) - 1 and buttons:
            payload["reply_markup"] = _markup(buttons)
        await _call("sendMessage", payload)
    return True


# ------------------------------------------------------------------ actions behind the buttons

async def handle_action(data: str) -> str:
    """Run a button's action. Returns the confirmation shown to the user."""
    from .apply import engine   # engine imports this module
    action, _, raw = data.partition(":")
    if action == "pse":                       # pause buttons: pse:<minutes> or pse:tomorrow / pse:off
        return pause.pause_now(raw)
    if action == "unp":
        return pause.resume_now()
    if action == "ans":                       # ans:<app>:<option index>
        app_raw, _, idx = raw.partition(":")
        if not (app_raw.isdigit() and idx.isdigit()):
            return "Unknown button."
        with db.session() as s:
            options = engine.last_block(s, int(app_raw)).get("options") or []
        if int(idx) >= len(options):
            return "That question has changed; open the application page."
        return answer(int(app_raw), options[int(idx)])
    if not raw.isdigit():
        return "Unknown button."
    i = int(raw)
    if action == "sub":
        return await engine.mark_submitted(i, via="telegram")
    if action == "skip":
        out = engine.remove_application(i, "Skipped via Telegram", via="telegram")
        asyncio.create_task(_continue_soon(10))
        return out
    if action == "rst":
        out = await engine.restart_application(i, via="telegram")
        if out.startswith("restarting"):
            asyncio.create_task(engine.run_after_restart(i))
        return out
    if action == "self":
        return engine.move_to_apply_yourself(i, via="telegram")
    if action == "res":
        out = engine.resume_blocked(i, via="telegram")
        asyncio.create_task(engine.run_when_free(i))
        return out
    if action == "apr":
        return engine.approve(i, via="telegram")
    if action == "dis":
        return engine.dismiss(i, via="telegram")
    return "Unknown button."


async def _continue_soon(seconds: int) -> None:
    from .apply import engine   # engine imports this module
    await asyncio.sleep(seconds)
    await engine.run_queue()


PAUSE_BUTTONS: Buttons = [[("1 hour", "pse:60"), ("3 hours", "pse:180")],
                          [("Until 9 AM", "pse:tomorrow"), ("Until I resume", "pse:off")]]


def answer(app_id: int, text: str) -> str:
    """The user's answer to the question an application stopped on: save it and continue."""
    from .apply import engine   # engine imports this module
    out = engine.resume_blocked(app_id, text, via="telegram")
    if out.startswith("Saved"):
        asyncio.create_task(engine.run_when_free(app_id))
    return out


def question_buttons(app_id: int, options: list[str]) -> Buttons:
    rows = [[(o[:40], f"ans:{app_id}:{i}") for i, o in enumerate(options[j:j + 2], start=j)]
            for j in range(0, min(len(options), 8), 2)]
    return rows + application_buttons(app_id, "needs_user")


def review_buttons(job_id: int) -> Buttons:
    return [[("✅ Approve", f"apr:{job_id}"), ("✖ Skip job", f"dis:{job_id}")]]


def application_buttons(app_id: int, status: str) -> Buttons:
    if status == "awaiting_review":
        return [[("✅ I submitted it", f"sub:{app_id}"), ("✖ Skip", f"skip:{app_id}")], [("↻ Restart", f"rst:{app_id}")]]
    if status == "apply_yourself":
        return [[("✅ I submitted it", f"sub:{app_id}"), ("✖ Skip", f"skip:{app_id}")]]
    if status == "needs_user":
        return [[("▶ Resume", f"res:{app_id}"), ("↻ Restart", f"rst:{app_id}")],
                [("🖐 Apply myself", f"self:{app_id}"), ("✖ Skip", f"skip:{app_id}")]]
    if status in ("failed", "timed_out"):
        return [[("↻ Restart", f"rst:{app_id}"), ("✖ Skip", f"skip:{app_id}")]]
    return []


# ------------------------------------------------------------------ commands

HELP = ("JobHelper bot\n"
        "/status – today's summary\n/week – the last 7 days\n"
        "/pending – what's waiting for you (with buttons)\n/review – top jobs to approve\n"
        "/pause – pause job search and applying (/pause 2h, /pause 30m, /pause tomorrow)\n"
        "/resume – start again now\n/help – this list")

COMMANDS = [("status", "Today's summary"), ("week", "The last 7 days"), ("pending", "What's waiting for you"),
            ("review", "Top jobs to approve"), ("pause", "Pause job search and applying"),
            ("resume", "Start again now"), ("help", "All commands")]


async def handle_command(text: str) -> None:

    cmd = text.split()[0].split("@")[0].lower()
    words = text.split(maxsplit=1)
    arg = words[1] if len(words) > 1 else ""
    if cmd == "/pause":
        if not arg:
            now = pause.current()
            head = (f"Already paused {now['label']}. Change it, or /resume:" if now
                    else "Pause job search and applying for how long?")
            await send(head, PAUSE_BUTTONS)
        else:
            await send(pause.pause_now(arg))
    elif cmd in ("/resume", "/unpause"):
        await send(pause.resume_now())
    elif cmd == "/status":
        await send(summary.daily_text())
    elif cmd == "/week":
        await send(summary.period_text(7))
    elif cmd == "/pending":
        with db.session() as s:
            rows = [(a.id, a.status, a.job.title, a.job.company, a.final_url or a.job.apply_url or a.job.url)
                    for a in s.scalars(select(db.Application).where(
                        db.Application.status.in_(["awaiting_review", "apply_yourself", "needs_user"]))
                        .order_by(db.Application.last_update_at.desc()))]
        if not rows:
            await send("Nothing is waiting for you. 🎉")
        for app_id, st, title, company, url in rows[:10]:
            label = {"awaiting_review": "📝 Review", "apply_yourself": "🖐 Apply yourself",
                     "needs_user": "⏸ Needs you"}[st]
            extra = f"\n{url}" if st == "apply_yourself" else ""
            await send(f"{label}: #{app_id} {title} · {company}{extra}", application_buttons(app_id, st))
    elif cmd == "/review":
        with db.session() as s:
            jobs = [(j.id, j.title, j.company, int(j.score or 0), (j.score_detail or {}).get("summary", ""))
                    for j in s.scalars(select(db.Job).where(db.Job.status == "review")
                                       .order_by(db.Job.score.desc()).limit(5))]
        if not jobs:
            await send("Review queue is empty.")
        for jid, title, company, score, summ in jobs:
            await send(f"[{score}] {title} · {company}\n{summ}", review_buttons(jid))
    else:
        await send(HELP)


# ------------------------------------------------------------------ polling loop

async def _handle_update(u: dict[str, Any]) -> None:
    st = _state()
    chat_id = st.get("chat_id")
    if "callback_query" in u:
        cq = u["callback_query"]
        msg = cq.get("message") or {}
        if not chat_id or (msg.get("chat") or {}).get("id") != chat_id:
            return                                            # not our user: ignore silently
        try:
            result = await handle_action(cq.get("data", ""))
        except Exception as exc:
            log.exception("button action failed")
            result = f"That didn't work: {exc}"
        await _call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": result[:190]})
        try:   # drop the buttons so the same action can't be tapped twice
            await _call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": msg["message_id"],
                                                   "reply_markup": {"inline_keyboard": []}})
        except Exception:
            pass
        await send(f"✔ {result}")
        return
    m = u.get("message") or {}
    text = (m.get("text") or "").strip()
    chat = (m.get("chat") or {}).get("id")
    if not text or chat is None:
        return
    if not chat_id:                                           # pairing: /start <code>
        parts = text.split()
        start = parts[0].split("@")[0] == "/start" and len(parts) > 1
        if start and not secrets.compare_digest(parts[1].upper().encode(), st.get("pair_code", "~").encode()):
            st["pair_fails"] = st.get("pair_fails", 0) + 1       # 5 wrong tries retire the code
            if st["pair_fails"] >= 5:
                st.pop("pair_code", None)
                st["pair_fails"] = 0
            _save(st)
        elif start:
            name = (m.get("from") or {}).get("first_name", "")
            _save({"chat_id": chat, "name": name, "bot_username": st.get("bot_username", "")})
            await send(f"Paired ✅ Hi {name}. JobHelper alerts will come here from now on.\n\n{HELP}")
        return
    if chat != chat_id:
        return                                                # only the paired chat is answered
    if text.startswith("/"):
        await handle_command(text)
        return
    asked = re.search(r"#(\d+) .* asks:", (m.get("reply_to_message") or {}).get("text") or "")
    if asked:                                                 # a reply to a question: that's the answer
        await send(f"✔ {answer(int(asked.group(1)), text)}")
    else:
        await send("To answer a question from an application, reply to its ❓ message.\n\n" + HELP)


async def run_forever() -> None:
    """Long-poll Telegram for messages and button taps. Started by the web app."""
    if not token():
        return
    delay = 30
    while True:
        try:
            me = await _call("getMe", {})
            st = _state()
            st["bot_username"] = me.get("username", "")
            st.pop("last_error", None)
            _save(st)
            if not st.get("chat_id"):
                pairing_code()
            log.info("Telegram bot connected")
            try:   # the command menu next to the message box
                await _call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
            except Exception as exc:
                log.warning("couldn't set the bot's command menu: %s", exc)
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Keep trying: Telegram can be slow to accept a freshly rotated token.
            log.error("Telegram bot not reachable yet (%s); retrying in %ss", exc, delay)
            st = _state()
            st["last_error"] = str(exc)
            _save(st)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 600)
    offset = None
    while True:
        try:
            payload = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                payload["offset"] = offset
            updates = await _call("getUpdates", payload, timeout=60)
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    await _handle_update(u)
                except Exception:
                    log.exception("telegram update failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("telegram poll error: %s", exc)
            await asyncio.sleep(10)
