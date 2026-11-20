"""JSON / text API for Hermes and other clients: bearer-token auth (except /api/health)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select

from .. import db, kb, pause, summary
from ..apply import engine
from .app import RUNNERS, _background, _continue_queue, _json, api_auth

router = APIRouter(prefix="/api")


@router.get("/summary", dependencies=[Depends(api_auth)])
async def api_summary(format: str = "text", days: int = 1):
    """days=1 → today's summary; days=7 → weekly report; any N works."""
    days = max(1, min(days, 90))
    if format == "text":
        return PlainTextResponse(summary.period_text(days))
    return JSONResponse(summary.stats() if days == 1 else
                        {"days": days, "applications": summary.applications_in_period(days)})


@router.get("/review", dependencies=[Depends(api_auth)])
async def api_review(limit: int = 20):
    with db.session() as s:
        jobs = s.scalars(select(db.Job).where(db.Job.status == "review").order_by(db.Job.score.desc()).limit(limit)).all()
        return [{"id": j.id, "title": j.title, "company": j.company, "score": j.score, "source": j.source,
                 "location": j.location, "url": j.url, "summary": (j.score_detail or {}).get("summary"),
                 "gaps": (j.score_detail or {}).get("gaps")} for j in jobs]


@router.get("/jobs/{job_id}", dependencies=[Depends(api_auth)])
async def api_job(job_id: int):
    with db.session() as s:
        j = s.get(db.Job, job_id)
        if j is None:
            raise HTTPException(404)
        return {"id": j.id, "title": j.title, "company": j.company, "status": j.status, "score": j.score,
                # No posting description: Hermes has a terminal, and a posting is text written by strangers.
                "score_detail": j.score_detail, "url": j.url, "location": j.location}


@router.post("/jobs/{job_id}/approve", dependencies=[Depends(api_auth)])
async def api_approve(job_id: int, request: Request):
    body = await _json(request)
    return {"result": engine.approve(job_id, body.get("note"), via="hermes")}


@router.post("/jobs/{job_id}/dismiss", dependencies=[Depends(api_auth)])
async def api_dismiss(job_id: int, request: Request):
    body = await _json(request)
    return {"result": engine.dismiss(job_id, body.get("note"), via="hermes")}


@router.get("/applications", dependencies=[Depends(api_auth)])
async def api_applications(status: str = "", limit: int = 30, days: int = 0):
    """days>0 → applications from the last N days with their current status."""
    if days > 0:
        return summary.applications_in_period(min(days, 90), status or None, limit)
    with db.session() as s:
        stmt = select(db.Application, db.Job).join(db.Job)
        if status:
            stmt = stmt.where(db.Application.status == status)
        rows = s.execute(stmt.order_by(db.Application.created_at.desc()).limit(limit)).all()
        return [{"id": a.id, "job_id": j.id, "title": j.title, "company": j.company, "portal": a.portal,
                 "status": a.status, "applied_at": a.applied_at, "blocker": a.blocker} for a, j in rows]


@router.post("/applications/{app_id}/submitted", dependencies=[Depends(api_auth)])
async def api_submitted(app_id: int):
    out = await engine.mark_submitted(app_id, via="hermes")
    _background("apply", _continue_queue())
    return {"result": out}


@router.post("/applications/{app_id}/skip", dependencies=[Depends(api_auth)])
async def api_skip(app_id: int, request: Request):
    body = await _json(request)
    out = engine.remove_application(app_id, body.get("reason") or "Skipped via Telegram", via="hermes")
    _background("apply", _continue_queue(10))
    return {"result": out}


@router.post("/applications/{app_id}/restart", dependencies=[Depends(api_auth)])
async def api_restart(app_id: int):
    out = await engine.restart_application(app_id, via="hermes")
    if out.startswith("restarting"):
        _background("apply", engine.run_after_restart(app_id))
    return {"result": out}


@router.post("/applications/{app_id}/resume", dependencies=[Depends(api_auth)])
async def api_resume_application(app_id: int):
    out = engine.resume_blocked(app_id, via="hermes")
    _background(f"resume_{app_id}", engine.run_when_free(app_id))
    return {"result": out}


@router.post("/kb/note", dependencies=[Depends(api_auth)])
async def api_note(request: Request):
    body = await _json(request)
    if not body.get("text"):
        raise HTTPException(400, "text required")
    kb.add_note(body["text"], source=body.get("source", "hermes"), topic=body.get("topic"))
    return {"result": "saved"}


@router.get("/kb/search", dependencies=[Depends(api_auth)])
async def api_kb_search(q: str):
    return kb.search(q)


@router.post("/run/{what}", dependencies=[Depends(api_auth)])
async def api_run(what: str):
    if what not in RUNNERS:
        raise HTTPException(404)
    _background(what, RUNNERS[what]())
    return {"result": f"started {what}"}


@router.get("/pause", dependencies=[Depends(api_auth)])
async def api_pause_status():
    p = pause.current()
    return {"paused": bool(p), "label": p["label"] if p else None}


@router.post("/pause", dependencies=[Depends(api_auth)])
async def api_pause(request: Request):
    """Body {"duration": "2h" | "30m" | "tomorrow" | "off" (until resumed)}."""
    body = await _json(request)
    out = pause.pause_now(str(body.get("duration", "60")), via="hermes")
    return {"result": out}


@router.post("/resume", dependencies=[Depends(api_auth)])
async def api_unpause():
    return {"result": pause.resume_now()}


@router.get("/health")
async def health():
    return {"ok": True}
