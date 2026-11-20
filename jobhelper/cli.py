"""`python -m jobhelper <command>`: server, one-off runs, verification, maintenance."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False) if not isinstance(obj, str) else obj)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):   # Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="jobhelper")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="dashboard + API + scheduler")
    d = sub.add_parser("discover", help="find jobs now")
    d.add_argument("--sources", nargs="*", help="boards linkedin naukri indeed email")
    d.add_argument("--no-score", action="store_true")
    sc = sub.add_parser("score", help="score prefiltered jobs")
    sc.add_argument("--limit", type=int, default=200)
    a = sub.add_parser("apply", help="run the apply queue now")
    a.add_argument("--max", type=int, default=1)
    a.add_argument("--job", type=int, help="queue and apply this job id right away")
    sub.add_parser("email", help="poll Gmail for responses now")
    sm = sub.add_parser("summary", help="print (or --send) the daily summary")
    sm.add_argument("--send", action="store_true")
    v = sub.add_parser("verify", help="run the harness verifier")
    v.add_argument("--llm", action="store_true", help="also run LLM evals (uses your subscription)")
    v.add_argument("--json", action="store_true")
    sub.add_parser("verify-boards", help="check configured ATS board tokens")
    sub.add_parser("doctor", help="check Claude auth, Gmail, Hermes, browser")
    sub.add_parser("browser", help="open the shared browser to log in to portals (local desktop)")
    ki = sub.add_parser("kb-import", help="import a Markdown file into the KB")
    ki.add_argument("path")
    ki.add_argument("--name", default="claude-export")
    sub.add_parser("kb-reindex")
    vs = sub.add_parser("vault", help="career-site logins")
    vs.add_argument("action", choices=["list", "show"])
    vs.add_argument("domain", nargs="?")
    ap = sub.add_parser("approve")
    ap.add_argument("job_id", type=int)
    ns = sub.add_parser("notify-test", help="send a test message through Hermes")
    ns.add_argument("text", nargs="?", default="JobHelper test message ✅")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and Telegram's Bot API puts the bot token in the URL.
    # Keep secrets out of the logs: only warnings and errors from the HTTP stack.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    from . import config
    config.ensure_dirs()

    if args.cmd == "serve":
        import uvicorn
        host, port = config.secrets().bind.rsplit(":", 1)
        # ws="none": the dashboard has no WebSockets. Caddy's forward_auth passes the noVNC
        # upgrade headers on to /auth/check; with WebSockets enabled, uvicorn would answer
        # that check with 403 and the remote browser could never connect.
        uvicorn.run("jobhelper.web.app:app", host=host, port=int(port), proxy_headers=True,
                    forwarded_allow_ips="127.0.0.1", log_level="info", ws="none")
        return 0
    if args.cmd == "discover":
        from . import pipeline
        _print(asyncio.run(pipeline.discover(args.sources, score=not args.no_score)))
    elif args.cmd == "score":
        from . import scoring
        _print(asyncio.run(scoring.score_pending(args.limit)))
    elif args.cmd == "apply":
        from .apply import engine
        if args.job:
            print(engine.approve(args.job, via="cli"))
        _print(asyncio.run(engine.run_queue(max_runs=args.max, ignore_window=True, ignore_pause=True)))
    elif args.cmd == "email":
        from . import tracking
        _print(asyncio.run(tracking.poll()))
    elif args.cmd == "summary":
        from . import summary
        if args.send:
            _print({"sent": asyncio.run(summary.send_daily())})
        else:
            print(summary.daily_text())
    elif args.cmd == "verify":
        from . import verify
        report = asyncio.run(verify.run(llm=args.llm))
        print(json.dumps(report, indent=2, default=str) if args.json else verify.render(report))
        return 0 if report["ok"] else 1
    elif args.cmd == "verify-boards":
        from .sources import ats
        _print(asyncio.run(ats.verify(config.settings().get("ats_boards") or {})))
    elif args.cmd == "doctor":
        from . import verify
        _print(asyncio.run(verify.doctor()))
    elif args.cmd == "browser":
        async def _open():
            from .browser import BROWSER
            ctx = await BROWSER.context()
            page = await ctx.new_page()
            for url in ("https://www.naukri.com/nlogin/login", "https://www.linkedin.com/login",
                        "https://secure.indeed.com/auth"):
                await page.goto(url)
                input(f"Log in at {url} in the browser window, then press Enter here… ")
            await BROWSER.close()
        asyncio.run(_open())
    elif args.cmd == "kb-import":
        from pathlib import Path
        from . import kb
        path = kb.import_document(args.name, Path(args.path).read_text(encoding="utf-8"),
                                  title="claude.ai export" if args.name == "claude-export" else args.name)
        print(f"imported → {path}")
    elif args.cmd == "kb-reindex":
        from . import kb
        print(f"{kb.reindex()} chunks indexed")
    elif args.cmd == "vault":
        from .apply import vault
        if args.action == "list":
            _print(vault.list_logins())
        else:
            _print(vault.get(args.domain or "") or "no login for that domain")
    elif args.cmd == "approve":
        from .apply import engine
        print(engine.approve(args.job_id, via="cli"))
    elif args.cmd == "notify-test":
        from . import notify
        _print({"sent": asyncio.run(notify.send(args.text, kind="test"))})
    return 0


if __name__ == "__main__":
    sys.exit(main())
