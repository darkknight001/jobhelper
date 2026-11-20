"""Alerts to Telegram through Hermes' webhook adapter (deliver_only route, no LLM cost).

Signature: Hermes "generic V2": X-Webhook-Signature-V2 = hex HMAC-SHA256 of
"<unix-ts>.<raw body>" with X-Webhook-Timestamp = <unix-ts> (5-minute replay window).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid

import httpx

from . import config, db

log = logging.getLogger(__name__)
TELEGRAM_LIMIT = 3800   # stay under Telegram's 4096 with room for Hermes' framing


def sign(secret: str, body: bytes, ts: str) -> str:
    return hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()


def _chunks(text: str) -> list[str]:
    if len(text) <= TELEGRAM_LIMIT:
        return [text]
    out, buf = [], ""
    for para in text.split("\n"):
        if len(buf) + len(para) + 1 > TELEGRAM_LIMIT:
            out.append(buf)
            buf = ""
        buf += para + "\n"
    if buf.strip():
        out.append(buf)
    return out


async def send(text: str, *, kind: str = "info", buttons=None) -> bool:
    """Deliver an alert. JobHelper's own Telegram bot when it's paired (with tap buttons);
    otherwise, or if the bot can't deliver, the Hermes webhook."""
    from . import telegram_bot   # telegram_bot -> summary -> notify
    if telegram_bot.active():
        try:
            await telegram_bot.send(text, buttons)
            with db.session() as s:
                db.add_event(s, "notification", text[:4000], detail={"kind": kind, "via": "jobhelper_bot"})
            return True
        except Exception as exc:
            log.error("JobHelper bot couldn't deliver (%s); falling back to Hermes", exc)
    return await _send_hermes(text, kind=kind)


async def _send_hermes(text: str, *, kind: str = "info") -> bool:
    sec = config.secrets()
    ok = True
    if not (sec.hermes_webhook_url and sec.hermes_webhook_secret):
        log.warning("Hermes webhook not configured; alert kept in the dashboard only:\n%s", text)
        ok = False
    else:
        async with httpx.AsyncClient(timeout=20) as client:
            for part in _chunks(text):
                body = json.dumps({"text": part, "kind": kind}, ensure_ascii=False).encode()
                ts = str(int(time.time()))
                headers = {"Content-Type": "application/json", "X-Webhook-Timestamp": ts,
                           "X-Webhook-Signature-V2": sign(sec.hermes_webhook_secret, body, ts),
                           "X-Request-ID": str(uuid.uuid4())}
                try:
                    r = await client.post(sec.hermes_webhook_url, content=body, headers=headers)
                    if r.status_code != 200:
                        log.error("Hermes webhook %s: %s", r.status_code, r.text[:300])
                        ok = False
                except httpx.HTTPError as exc:
                    log.error("Hermes webhook unreachable: %s", exc)
                    ok = False
    with db.session() as s:
        db.add_event(s, "notification" if ok else "notification_failed", text[:4000], detail={"kind": kind})
    return ok


def link(path: str) -> str:
    return f"{config.secrets().public_url}{path}"
