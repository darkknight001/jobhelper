"""Claude through the Agent SDK, authenticated with the user's subscription.

Auth: CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`). If it's unset, the bundled
Claude Code falls back to whatever login exists on the machine. ANTHROPIC_API_KEY is
deliberately stripped so a stray key never moves usage onto API billing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

from . import config, usage
from .browser import secret_env_names

log = logging.getLogger(__name__)

# Subscription plans rate-limit concurrent use; keep it modest.
_sem = asyncio.Semaphore(2)


class LLMError(RuntimeError):
    pass


class LLMDeferred(LLMError):
    """The usage governor said not now (subscription limit headroom). Retry later."""


def base_env() -> dict[str, str]:
    # The CLI inherits os.environ; blank every secret it doesn't need (mail, vault, web, Telegram, Hermes, VNC).
    env = {k: "" for k in secret_env_names() if k.upper() != "CLAUDE_CODE_OAUTH_TOKEN"}
    env.update({"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
    token = config.secrets().claude_oauth_token
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def _scratch_cwd() -> str:
    # Run the CLI outside the repo so it never picks up a CLAUDE.md or project settings.
    d = config.DATA_DIR / "claude-cwd"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


async def structured(prompt: str, schema: dict[str, Any], *, system: str, model: str,
                     effort: str | None = "medium", max_turns: int = 6, task: str = "scoring",
                     ref: str | None = None) -> Any:
    """One-shot call that returns JSON validated against `schema`. No tools.

    Raises LLMDeferred, without calling Claude, when the governor is pausing `task`.
    """
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system,
        tools=[],
        allowed_tools=[],
        setting_sources=[],
        permission_mode="dontAsk",
        output_format={"type": "json_schema", "schema": schema},
        max_turns=max_turns,
        cwd=_scratch_cwd(),
        env=base_env(),
        effort=effort,
    )
    async with _sem:
        # Checked after the wait: calls queued behind others must see the usage those spent.
        ok, why = usage.allowed(task)
        if not ok:
            raise LLMDeferred(why)
        result: ResultMessage | None = None
        text_parts: list[str] = []
        try:
            async for msg in query(prompt=prompt, options=options):
                usage.observe(msg)
                if isinstance(msg, AssistantMessage):
                    text_parts += [b.text for b in msg.content if isinstance(b, TextBlock)]
                elif isinstance(msg, ResultMessage):
                    result = msg
        except Exception as exc:  # the SDK raises after yielding an error result
            usage.record(result, task=task, ref=ref)
            if usage.LIMIT_TEXT.search(str(exc)):
                usage.note_limit_hit(str(exc))
                raise LLMDeferred(f"Claude limit hit: {exc}") from exc
            if result is None or result.subtype == "success":
                raise LLMError(f"Claude call failed: {exc}") from exc
        else:
            usage.record(result, task=task, ref=ref)
    if result is None:
        raise LLMError("Claude returned no result")
    if result.subtype != "success" or result.structured_output is None:
        raise LLMError(f"Claude result {result.subtype}: {result.errors or result.result or ''}")
    return result.structured_output


async def check() -> dict[str, Any]:
    """Health check used by `jobhelper doctor`: proves auth works and which model answered."""
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    out = await structured('Return {"ok": true}.', schema, system="You are a health check.",
                           model=config.settings()["llm"]["scoring_model"], effort="low", task="health")
    return {"ok": bool(out.get("ok")), "auth": "oauth-token" if config.secrets().claude_oauth_token
            else "local-login", "env_has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY"))}


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
