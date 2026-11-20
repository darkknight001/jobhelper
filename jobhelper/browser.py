"""One shared, persistent, headed Chromium.

Discovery code drives it through Playwright; the apply agent attaches the same browser
through Playwright MCP over CDP, so both see the same logged-in sessions. `lock` makes
sure only one of them drives it at a time. Child processes get the environment with secrets
scrubbed (`child_env`).
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from . import config

log = logging.getLogger(__name__)

# CLAUDE_CODE_OAUTH_TOKEN, GMAIL_APP_PASSWORD, JOBHELPER_VAULT_KEY, *_SESSION_SECRET, TELEGRAM_BOT_TOKEN, ...
SECRET_ENV = re.compile(r"(TOKEN|PASSWORD|PASSWD|SECRET|_KEY)$", re.I)


def secret_env_names() -> list[str]:
    return [k for k in os.environ if SECRET_ENV.search(k)]


def child_env() -> dict[str, str]:
    """The environment without secrets, for processes that load web pages (they need none)."""
    return {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}


class SharedBrowser:
    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._attached = None          # set when we connected to another process's Chromium
        self._start_lock = asyncio.Lock()
        self.lock = asyncio.Lock()   # exclusive use: discovery page work or one apply run

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{config.secrets().cdp_port}"

    async def context(self) -> BrowserContext:
        async with self._start_lock:
            if self._ctx is not None:
                try:
                    _ = self._ctx.pages  # raises if the browser went away
                    return self._ctx
                except Exception:
                    log.warning("browser context lost; relaunching")
                    self._ctx = None
            if self._pw is None:
                self._pw = await async_playwright().start()
            # Another process (the dashboard, or a CLI run) may already own the profile.
            # Chromium allows one instance per profile, so attach to it rather than launch.
            try:
                browser = await self._pw.chromium.connect_over_cdp(self.cdp_url, timeout=3000)
                if browser.contexts:
                    self._ctx, self._attached = browser.contexts[0], browser
                    log.info("attached to the running browser at %s", self.cdp_url)
                    return self._ctx
                await browser.close()
            except Exception:
                pass
            config.BROWSER_DIR.mkdir(parents=True, exist_ok=True)
            port = config.secrets().cdp_port
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(config.BROWSER_DIR),
                headless=config.secrets().headless,
                viewport=None,
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                accept_downloads=False,
                env=child_env(),   # a renderer exploit must not find tokens in /proc/self/environ
                args=[
                    f"--remote-debugging-port={port}",
                    "--remote-debugging-address=127.0.0.1",
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--start-maximized",
                    "--window-position=0,0",
                    "--window-size=1440,900",   # fills the Xvfb screen; there's no window manager
                ],
            )
            self._ctx.on("close", lambda _: setattr(self, "_ctx", None))
            log.info("browser up (profile %s, CDP %s)", config.BROWSER_DIR, self.cdp_url)
            return self._ctx

    @asynccontextmanager
    async def page(self) -> AsyncIterator[Page]:
        ctx = await self.context()
        page = await ctx.new_page()
        try:
            yield page
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def close(self) -> None:
        if self._attached is not None:
            # We only borrowed someone else's browser: disconnect, never close their window.
            try:
                await self._attached.close()
            except Exception:
                pass
        elif self._ctx:
            await self._ctx.close()
        if self._pw:
            await self._pw.stop()
        self._ctx, self._pw, self._attached = None, None, None


BROWSER = SharedBrowser()


async def human_pause(lo: float = 1.2, hi: float = 3.5) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


async def is_challenge(page: Page) -> str | None:
    """Detect bot walls we must not try to beat. Returns a description or None."""
    try:
        title = (await page.title()).lower()
        body = (await page.inner_text("body", timeout=3000)).lower()[:4000]
    except Exception:
        return None
    if "just a moment" in title or "verify you are human" in body or "cf-challenge" in body:
        return "Cloudflare / human verification page"
    if "captcha" in body and ("verify" in body or "robot" in body):
        return "CAPTCHA"
    if "access denied" in title or "unusual traffic" in body:
        return "access denied / unusual traffic"
    return None
