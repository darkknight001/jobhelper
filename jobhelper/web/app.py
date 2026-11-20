"""Dashboard (session login) + JSON/text API for Hermes (bearer token).

Form posts must be same-origin, repeated failed logins lock the client out, and prep pages
get a strict CSP.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import logging
import time
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from typing import Any

import markdown as md
from markupsafe import Markup, escape
import yaml
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from starlette.middleware.sessions import SessionMiddleware

from .. import (assistant, companies, config, db, kb, notify, pause, pipeline, prep, scheduler, summary,
               telegram_bot, tracking, usage)
from ..browser import BROWSER
from ..apply import engine, vault

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
# Asset version for ?v= on CSS/JS links, so a deploy never leaves browsers on a cached old copy.
templates.env.globals["v"] = int(max(p.stat().st_mtime for p in (HERE / "static").iterdir()))


def _ist(dt, fmt: str = "%d %b %H:%M") -> str:
    return dt.astimezone(config.tz()).strftime(fmt) if dt else ""


templates.env.filters["ist"] = _ist


def _tok(n) -> str:
    n = int(n or 0)
    return f"{n / 1e6:.1f}M" if n >= 1e6 else (f"{n / 1e3:.0f}k" if n >= 1e3 else str(n))


templates.env.filters["tok"] = _tok


def _http_url(u) -> str:
    """Only http(s) URLs become links; anything else (javascript:, data:) turns into "#"."""
    return u if isinstance(u, str) and u.lower().startswith(("http://", "https://")) else "#"


templates.env.filters["http_url"] = _http_url


def _company_rating(name: str) -> str | None:
    with db.session() as s:
        return companies.describe(s.get(db.CompanyRating, companies.norm(name)))


templates.env.globals["company_rating"] = _company_rating


def _company_badge(name: str):
    """Coloured 1/3/5 badge shown next to the score on list pages ("–" when unrated)."""
    cr = _company_rating(name)
    if not cr:
        return Markup('<div class="crate" title="Company not rated (too few AmbitionBox reviews)">–</div>')
    n = cr[0]
    return Markup(f'<div class="crate r{n}" title="Company rating, AmbitionBox: {escape(cr)}">★{n}</div>')


templates.env.globals["company_badge"] = _company_badge
_bg: set[asyncio.Task] = set()
STATE: dict[str, Any] = {}


def _background(name: str, coro) -> None:
    async def wrap():
        try:
            STATE[f"last_{name}"] = {"status": "running"}
            out = await coro
            STATE[f"last_{name}"] = {"status": "done", "result": out}
        except Exception as exc:
            log.exception("%s failed", name)
            STATE[f"last_{name}"] = {"status": "error", "error": str(exc)}
    t = asyncio.create_task(wrap())
    _bg.add(t)
    t.add_done_callback(_bg.discard)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    db.engine()
    kb.reindex()
    stranded = engine.recover_stranded()
    if stranded:
        log.info("re-queued %d application(s) left mid-run by the last shutdown", stranded)
    sch = None
    if os.environ.get("JOBHELPER_NO_SCHEDULER") != "1":
        sch = scheduler.build()
        sch.start()
        STATE["scheduler"] = sch
    bot_task = asyncio.create_task(telegram_bot.run_forever()) if telegram_bot.token() else None
    yield
    if bot_task:
        bot_task.cancel()
    if sch:
        sch.shutdown(wait=False)
    await BROWSER.close()


app = FastAPI(title="JobHelper", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=config.secrets().session_secret, session_cookie="jh_session",
                   max_age=60 * 60 * 24 * 30, same_site="lax", https_only=config.secrets().public_url.startswith("https"))


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    """CSRF: a state-changing browser request must come from this site. Browsers always send Origin on
    cross-site POSTs (Referer is the fallback); a request with neither can't be a browser form post, so it
    is let through (scripts/TestClient, the bearer-token /api/* is exempt anyway)."""
    if request.method not in ("GET", "HEAD", "OPTIONS") and not request.url.path.startswith("/api/"):
        src = request.headers.get("origin") or request.headers.get("referer")
        if src:
            allowed = {request.headers.get("host", ""), urlparse(config.secrets().public_url).netloc}
            if urlparse(src).netloc not in allowed:
                return PlainTextResponse("Cross-site request refused", status_code=403)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


# ------------------------------------------------------------------ auth

def _local(url: str, default: str = "/") -> str:
    """Redirect only within the dashboard ("//x" and "/\\x" are other sites to a browser)."""
    return url if url.startswith("/") and not url.startswith(("//", "/\\")) else default


def _logged_in(request: Request) -> bool:
    return bool(request.session.get("ok"))


def page_auth(request: Request) -> None:
    if not _logged_in(request):
        raise HTTPException(status_code=303, headers={"Location": f"/login?next={request.url.path}"})


def api_auth(request: Request) -> None:
    token = config.secrets().api_token
    got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if _logged_in(request):
        return
    if not token or not hmac.compare_digest(got, token):
        raise HTTPException(status_code=401, detail="bad token")


# shortcut: in-memory per-IP counts reset on restart; behind Caddy every client may share one IP.
LOGIN_MAX_FAILS, LOGIN_WINDOW = 10, 15 * 60
_LOGIN_FAILS: dict[str, list[float]] = {}


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": None})


@app.post("/login")
async def login(request: Request, password: str = Form(...), next: str = Form("/")):
    expected = config.secrets().dashboard_password
    ip = request.client.host if request.client else "?"
    now = time.monotonic()
    fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < LOGIN_WINDOW]
    _LOGIN_FAILS[ip] = fails
    if len(fails) >= LOGIN_MAX_FAILS:
        return templates.TemplateResponse(request, "login.html", {"next": next, "error": "Too many attempts; try again later"},
                                          status_code=429)
    if expected and hmac.compare_digest(password.encode(), expected.encode()):
        _LOGIN_FAILS.pop(ip, None)
        request.session["ok"] = True
        return RedirectResponse(_local(next), status_code=303)
    fails.append(now)
    await asyncio.sleep(1.5)
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": "Wrong password"},
                                      status_code=401)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/auth/check")
async def auth_check(request: Request):
    """Caddy forward_auth target for /vnc/*."""
    return PlainTextResponse("ok") if _logged_in(request) else PlainTextResponse("no", status_code=401)


# ------------------------------------------------------------------ pages

def vnc_url() -> str:
    """noVNC link for logged-in pages. The VNC password rides in the URL fragment, which
    browsers never send to the server, so it stays out of Caddy logs."""
    pw = config.secrets().vnc_password
    return "/vnc/vnc.html?autoconnect=1&resize=scale&path=vnc/websockify" + (f"#password={quote(pw)}" if pw else "")


def _ctx(request: Request, **kw) -> dict[str, Any]:
    s = config.settings()
    return {"request": request, "dry_run": s["apply"].get("dry_run", True), "public_url": config.secrets().public_url,
            "nav": request.url.path, "vnc_url": vnc_url(), "paused": pause.current(),
            "chat": _logged_in(request), **kw}


def _bars(series: list[dict[str, Any]]) -> dict[str, Any]:
    peak = max([1] + [d["applied"] for d in series] + [d["found"] for d in series])
    return {"peak": peak, "series": series}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(page_auth)])
async def home(request: Request):
    st = summary.stats()
    cap = config.settings()["apply"]["daily_cap_per_portal"]
    portals = [{"name": p, "today": st["applied_today"].get(p, 0), "cap": cap}
               for p in ("naukri", "linkedin", "indeed", "company")]
    with db.session() as s:
        events = s.scalars(select(db.Event).where(db.Event.kind.not_in(["notification"]))
                           .order_by(db.Event.at.desc()).limit(15)).all()
        runs = s.scalars(select(db.Run).order_by(db.Run.started_at.desc()).limit(6)).all()
        events = [{"at": e.at, "kind": e.kind, "summary": e.summary, "application_id": e.application_id} for e in events]
        runs = [{"kind": r.kind, "at": r.started_at, "ok": r.ok, "error": r.error} for r in runs]
    sch = STATE.get("scheduler")
    next_runs = ({j.id: j.next_run_time for j in sch.get_jobs()} if sch else {})
    return templates.TemplateResponse(request, "home.html", _ctx(
        request, st=st, portals=portals, events=events, runs=runs, next_runs=next_runs, chart=_bars(st["series"]),
        claude=usage.today()))


PER_PAGE = {"jobs": 50, "applications": 50, "review": 20}


def _search(stmt, q: str):
    """Title / company contains q (case-insensitive)."""
    like = f"%{q.strip()}%"
    return stmt.where(or_(db.Job.title.ilike(like), db.Job.company.ilike(like))) if q.strip() else stmt


def _paged(s, stmt, page: int, per: int):
    """(rows, total, page) with page clamped to the last page that has rows."""
    total = s.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    page = max(1, min(page, (total + per - 1) // per or 1))
    return s.execute(stmt.offset((page - 1) * per).limit(per)).all(), total, page


@app.get("/review", response_class=HTMLResponse, dependencies=[Depends(page_auth)])
async def review(request: Request, q: str = "", page: int = 1):
    with db.session() as s:
        stmt = _search(select(db.Job).where(db.Job.status == "review"), q)
        rows, total, page = _paged(s, stmt.order_by(db.Job.priority.desc(), db.Job.score.desc()), page, PER_PAGE["review"])
    back = "/review" + (f"?{request.url.query}" if request.url.query else "")
    return templates.TemplateResponse(request, "review.html", _ctx(
        request, jobs=[r[0] for r in rows], total=total, page=page, per=PER_PAGE["review"], q=q, back=back))


@app.post("/jobs/{job_id}/approve", dependencies=[Depends(page_auth)])
async def approve_page(job_id: int, note: str = Form(""), back: str = Form("/review")):
    engine.approve(job_id, note or None)
    return RedirectResponse(_local(back, "/review"), status_code=303)


@app.post("/jobs/{job_id}/dismiss", dependencies=[Depends(page_auth)])
async def dismiss_page(job_id: int, note: str = Form(""), back: str = Form("/review")):
    engine.dismiss(job_id, note or None)
    return RedirectResponse(_local(back, "/review"), status_code=303)


JOB_TABS = ["review", "queued_auto", "approved", "applied", "dry_run_ok", "needs_user", "skipped",
            "filtered_out", "dismissed", "failed", "prefiltered"]


@app.get("/jobs", response_class=HTMLResponse, dependencies=[Depends(page_auth)])
async def jobs(request: Request, status: str = "", source: str = "", q: str = "", page: int = 1):
    with db.session() as s:
        stmt = _search(select(db.Job), q)
        if status:
            stmt = stmt.where(db.Job.status == status)
        if source:
            stmt = stmt.where(db.Job.source == source)
        rows, total, page = _paged(s, stmt.order_by(db.Job.score.desc().nullslast(), db.Job.discovered_at.desc()),
                                   page, PER_PAGE["jobs"])
        counts = dict(s.execute(select(db.Job.status, func.count()).group_by(db.Job.status)).all())
    return templates.TemplateResponse(request, "jobs.html", _ctx(
        request, jobs=[r[0] for r in rows], total=total, page=page, per=PER_PAGE["jobs"], status=status,
        source=source, q=q, tabs=JOB_TABS, counts=counts))


@app.get("/jobs/{job_id}", response_class=HTMLResponse, dependencies=[Depends(page_auth)])
async def job_detail(request: Request, job_id: int):
    with db.session() as s:
        job = s.get(db.Job, job_id)
        if job is None:
            raise HTTPException(404)
        apps = list(job.applications)
        events = s.scalars(select(db.Event).where(db.Event.job_id == job_id).order_by(db.Event.at)).all()
    return templates.TemplateResponse(request, "job.html", _ctx(request, job=job, apps=apps, events=events))


@app.get("/applications", response_class=HTMLResponse, dependencies=[Depends(page_auth)])
async def applications(request: Request, status: str = "", q: str = "", page: int = 1):
    with db.session() as s:
        stmt = _search(select(db.Application, db.Job).join(db.Job), q)
        if status:
            stmt = stmt.where(db.Application.status == status)
        rows, total, page = _paged(s, stmt.order_by(func.coalesce(db.Application.applied_at, db.Application.created_at).desc()),
                                   page, PER_PAGE["applications"])
        counts = dict(s.execute(select(db.Application.status, func.count()).group_by(db.Application.status)).all())
    return templates.TemplateResponse(request, "applications.html", _ctx(
        request, rows=rows, total=total, page=page, per=PER_PAGE["applications"], status=status, q=q, counts=counts))


@app.get("/applications/{app_id}", response_class=HTMLResponse, dependencies=[Depends(page_auth)])
async def application_detail(request: Request, app_id: int, msg: str = ""):
    with db.session() as s:
        a = s.get(db.Application, app_id)
        if a is None:
            raise HTTPException(404)
        job, events = a.job, list(a.events)
        block = engine.last_block(s, app_id) if a.status == "needs_user" else {}
    has_prep = prep.path_for(app_id).exists()
    shot = (config.ARTIFACT_DIR / f"app-{app_id}" / "final.png").exists()
    submitted_shot = (config.ARTIFACT_DIR / f"app-{app_id}" / "submitted.png").exists()
    return templates.TemplateResponse(request, "application.html", _ctx(
        request, a=a, job=job, events=events, has_prep=has_prep, shot=shot, submitted_shot=submitted_shot,
        hold_hours=config.settings()["apply"].get("review_hold_hours", 12),
        removable=a.status in engine.REMOVABLE, skip_reasons=engine.SKIP_REASONS,
        restartable=a.status in engine.RESTARTABLE, block=block, msg=msg))


@app.post("/applications/{app_id}/apply-yourself", dependencies=[Depends(page_auth)])
async def apply_yourself_page(app_id: int):
    engine.move_to_apply_yourself(app_id)
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


@app.post("/applications/{app_id}/resume", dependencies=[Depends(page_auth)])
async def resume_application_page(app_id: int):
    engine.resume_blocked(app_id)
    _background(f"resume_{app_id}", engine.run_when_free(app_id))
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


@app.post("/applications/{app_id}/answer", dependencies=[Depends(page_auth)])
async def answer_page(app_id: int, answer: str = Form(""), choice: str = Form("")):
    out = engine.resume_blocked(app_id, (choice or answer).strip(), via="dashboard") if (choice or answer).strip() \
        else "Type an answer first."
    if out.startswith("Saved"):
        _background(f"resume_{app_id}", engine.run_when_free(app_id))
    return RedirectResponse(f"/applications/{app_id}?msg={quote(out)}", status_code=303)


async def _continue_queue(after_seconds: int = 120):
    """After a review decision, resume the queue shortly (time to glance at the confirmation)."""
    await asyncio.sleep(after_seconds)
    return await engine.run_queue()


@app.post("/applications/{app_id}/submitted", dependencies=[Depends(page_auth)])
async def submitted_page(app_id: int):
    await engine.mark_submitted(app_id, via="dashboard")
    _background("apply", _continue_queue())
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


@app.post("/applications/{app_id}/skip", dependencies=[Depends(page_auth)])
async def skip_page(app_id: int, reason: str = Form(""), other: str = Form("")):
    why = other.strip() if reason == "Other" and other.strip() else (reason or "Skipped after review")
    engine.remove_application(app_id, why, via="dashboard")
    _background("apply", _continue_queue(10))
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


@app.post("/applications/{app_id}/restart", dependencies=[Depends(page_auth)])
async def restart_page(app_id: int):
    await engine.restart_application(app_id, via="dashboard")
    _background("apply", engine.run_after_restart(app_id))
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


@app.post("/applications/{app_id}/prep", dependencies=[Depends(page_auth)])
async def prep_page(app_id: int):
    _background("prep", prep.ensure_brief(app_id, trigger="requested from dashboard", force=True))
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


@app.get("/prep/{app_id}", response_class=HTMLResponse, dependencies=[Depends(page_auth)])
async def prep_view(request: Request, app_id: int):
    p = prep.path_for(app_id)
    if not p.exists():
        raise HTTPException(404, "No prep brief yet")
    with db.session() as s:
        a = s.get(db.Application, app_id)
        job = a.job if a else None
    # The brief is model output built from job posts and web pages: render Markdown only, drop
    # raw HTML, and forbid scripts on this page (covers javascript: links too).
    renderer = md.Markdown(extensions=["tables", "fenced_code", "sane_lists"])
    renderer.preprocessors.deregister("html_block")
    renderer.inlinePatterns.deregister("html")
    html = renderer.convert(p.read_text(encoding="utf-8"))
    resp = templates.TemplateResponse(request, "prep.html", _ctx(request, html=html, job=job, app_id=app_id))
    resp.headers["Content-Security-Policy"] = ("default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                   "form-action 'self'; base-uri 'none'; script-src 'none'")
    return resp


@app.get("/artifacts/{app_id}/{name}", dependencies=[Depends(page_auth)])
async def artifact(app_id: int, name: str):
    if name not in ("final.png", "submitted.png"):
        raise HTTPException(404)
    p = config.ARTIFACT_DIR / f"app-{app_id}" / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/kb", response_class=HTMLResponse, dependencies=[Depends(page_auth)])
async def kb_page(request: Request, msg: str = ""):
    resumes = sorted(p.name for p in config.RESUME_DIR.glob("*.pdf"))
    profile_text = config.profile_path().read_text(encoding="utf-8")
    missing = _missing_profile_fields(config.profile())
    return templates.TemplateResponse(request, "kb.html", _ctx(
        request, docs=kb.list_docs(), resumes=resumes, profile_text=profile_text, msg=msg, missing=missing,
        logins=_safe_logins(), profile_version=_profile_version(profile_text)))


def _profile_version(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _safe_logins():
    try:
        return vault.list_logins()
    except Exception:
        return []


def _missing_profile_fields(p: dict[str, Any], prefix: str = "") -> list[str]:
    important = {"compensation.current_ctc_lpa", "compensation.expected_ctc_lpa", "availability.notice_period_days",
                 "preferences.willing_to_relocate", "links.linkedin", "availability.serving_notice",
                 "location.address_line1"}
    out = []
    for k, v in p.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out += _missing_profile_fields(v, key + ".")
        elif v in (None, "") and key in important:
            out.append(key)
    return out


@app.post("/kb/note", dependencies=[Depends(page_auth)])
async def kb_note(text: str = Form(...), topic: str = Form("")):
    kb.add_note(text, source="dashboard", topic=topic or None)
    return RedirectResponse("/kb?msg=Note+saved", status_code=303)


@app.post("/kb/import", dependencies=[Depends(page_auth)])
async def kb_import(content: str = Form(...), name: str = Form("claude-export")):
    kb.import_document(name, content, title="claude.ai export" if name == "claude-export" else name,
                       tags=["imported"])
    return RedirectResponse("/kb?msg=Imported", status_code=303)


@app.post("/kb/resume", dependencies=[Depends(page_auth)])
async def kb_resume(file: UploadFile = File(...)):
    name = Path(file.filename or "resume.pdf").name
    if not name.lower().endswith(".pdf"):
        return RedirectResponse("/kb?msg=PDF+only", status_code=303)
    dest = config.RESUME_DIR / name
    dest.write_bytes(await file.read())
    try:
        kb.import_document(f"resume-{dest.stem}", kb.resume_text(dest), title=f"Resume variant: {name}", tags=["resume"])
    except Exception:
        pass
    return RedirectResponse("/kb?msg=Resume+uploaded", status_code=303)


@app.post("/kb/profile", dependencies=[Depends(page_auth)])
async def kb_profile(profile_text: str = Form(...), profile_version: str = Form("")):
    try:
        data = yaml.safe_load(profile_text)
        assert isinstance(data, dict)
    except Exception:
        return RedirectResponse("/kb?msg=Profile+not+saved:+invalid+YAML", status_code=303)
    # Refuse a save made from a stale page, so it can't silently undo changes made since
    # (by JobHelper, Hermes, or another tab).
    current = config.profile_path().read_text(encoding="utf-8")
    if profile_version != _profile_version(current):
        return RedirectResponse("/kb?msg=Not+saved:+the+profile+changed+since+you+opened+this+page.+"
                                "Copy+your+edit,+reload,+and+apply+it+again.", status_code=303)
    config.profile_path().write_text(profile_text.replace("\r\n", "\n"), encoding="utf-8")
    return RedirectResponse("/kb?msg=Profile+saved", status_code=303)


@app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(page_auth)])
async def settings_page(request: Request, msg: str = ""):
    return templates.TemplateResponse(request, "settings.html", _ctx(request, s=config.settings(), msg=msg,
                                                                     state=STATE, tg=telegram_bot.status()))


@app.post("/telegram/test", dependencies=[Depends(page_auth)])
async def telegram_test():
    ok = await telegram_bot.send("✅ Test from JobHelper: alerts reach you here.\n\n" + telegram_bot.HELP)
    return RedirectResponse("/settings?msg=" + ("Test+sent" if ok else "Bot+not+paired+yet"), status_code=303)


@app.post("/pause", dependencies=[Depends(page_auth)])
async def pause_page(duration: str = Form("60")):
    out = pause.pause_now(duration, via="dashboard")
    if out.startswith("⏸"):
        await notify.send(out, kind="pause")
    return RedirectResponse("/settings?msg=" + quote(out.split(".")[0]), status_code=303)


@app.post("/resume", dependencies=[Depends(page_auth)])
async def unpause_page(back: str = Form("/")):
    out = pause.resume_now()
    if out.startswith("▶"):
        await notify.send(out, kind="pause")
    return RedirectResponse(_local(back), status_code=303)


@app.post("/telegram/unpair", dependencies=[Depends(page_auth)])
async def telegram_unpair():
    telegram_bot.unpair()
    return RedirectResponse("/settings?msg=Unpaired;+alerts+go+through+Hermes+again", status_code=303)


@app.post("/settings", dependencies=[Depends(page_auth)])
async def settings_save(dry_run: str = Form("off"), hold_for_review: str = Form("off"), auto_apply_min: int = Form(...),
                        review_min: int = Form(...), daily_cap: int = Form(...)):
    config.set_override("apply.dry_run", dry_run == "on")
    config.set_override("apply.hold_for_review", hold_for_review == "on")
    config.set_override("scoring.auto_apply_min", max(0, min(100, auto_apply_min)))
    config.set_override("scoring.review_min", max(0, min(100, review_min)))
    config.set_override("apply.daily_cap_per_portal", max(0, min(50, daily_cap)))
    return RedirectResponse("/settings?msg=Saved", status_code=303)


LOGIN_PAGES = ("https://www.naukri.com/nlogin/login", "https://www.linkedin.com/login",
               "https://secure.indeed.com/auth")


async def open_login_tabs() -> str:
    """Start the shared Chromium (if needed) with the portal login pages, for noVNC."""
    ctx = await BROWSER.context()
    for url in LOGIN_PAGES:
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
    return f"opened {len(LOGIN_PAGES)} login tabs"


RUNNERS = {
    "browser": lambda: open_login_tabs(),
    "discover": lambda: pipeline.discover(),
    "apply": lambda: engine.run_queue(ignore_window=True, ignore_pause=True),
    "email": lambda: tracking.poll(),
    "summary": lambda: summary.send_daily(),
}


@app.post("/run/{what}", dependencies=[Depends(page_auth)])
async def run_page(what: str):
    if what not in RUNNERS:
        raise HTTPException(404)
    _background(what, RUNNERS[what]())
    return RedirectResponse("/settings?msg=Started+" + what, status_code=303)


# ------------------------------------------------------------------ chat assistant

@app.post("/assistant/chat", dependencies=[Depends(page_auth)])
async def assistant_chat(request: Request):
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(415, "JSON only")
    body = await _json(request)
    try:
        return await assistant.reply(body.get("messages"))
    except ValueError as exc:
        return JSONResponse({"reply": f"Can't send that: {exc}.", "proposals": []}, status_code=400)
    except Exception:
        log.exception("assistant failed")
        return {"reply": "Something went wrong on my side. Try again in a minute.", "proposals": []}


@app.post("/assistant/confirm/{pid}", dependencies=[Depends(page_auth)])
async def assistant_confirm(pid: str):
    try:
        return {"result": await assistant.execute(pid)}
    except ValueError as exc:
        return {"result": f"Not done: {exc}"}


async def _json(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    if exc.status_code == 303 and exc.headers:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return HTMLResponse(f"<h1>{exc.status_code}</h1><p>{escape(str(exc.detail))}</p><p><a href='/'>Home</a></p>",
                        status_code=exc.status_code)


# The Hermes API lives in api.py; it uses the helpers above, so it's imported last.
from .api import router as api_router  # noqa: E402

app.include_router(api_router)
