"""SQLite storage: jobs, applications, events, KB index and run log."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, TypeDecorator, UniqueConstraint,
    create_engine, event, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from . import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """SQLite drops UTC offsets, so store naive UTC and hand back aware UTC.

    Binding an aware datetime in any zone (e.g. "start of today, IST") converts it to UTC
    first, so comparisons are always like-for-like.
    """
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None else None


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON, datetime: UTCDateTime}


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_job_source_ext"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)       # naukri|linkedin|indeed|greenhouse|lever|ashby|email
    external_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text)               # company site, when the portal redirects
    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str] = mapped_column(String(200), index=True)
    location: Mapped[str | None] = mapped_column(String(300))
    remote: Mapped[bool] = mapped_column(Boolean, default=False)
    experience: Mapped[str | None] = mapped_column(String(64))
    salary: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    skills: Mapped[list[Any]] = mapped_column(default=list)
    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    via: Mapped[str | None] = mapped_column(String(64))               # search|alert:naukri-priority|board
    priority: Mapped[bool] = mapped_column(Boolean, default=False)    # came from a Priority Applicant alert
    easy_apply: Mapped[bool | None] = mapped_column(Boolean)

    # Pipeline state, see docs/DESIGN.md "Job states".
    status: Mapped[str] = mapped_column(String(24), default="new", index=True)
    filter_reason: Mapped[str | None] = mapped_column(String(300))
    score: Mapped[float | None] = mapped_column(Float, index=True)
    score_detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    scored_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    resume_variant: Mapped[str | None] = mapped_column(String(200))
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    review_note: Mapped[str | None] = mapped_column(Text)

    applications: Mapped[list["Application"]] = relationship(back_populates="job", cascade="all, delete-orphan")

    @property
    def portal(self) -> str:
        return self.source


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    portal: Mapped[str] = mapped_column(String(32), index=True)       # counts against this portal's cap
    mode: Mapped[str] = mapped_column(String(16))                     # auto|approved
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    # queued|applying|applied|dry_run_ok|needs_user|failed + response states
    # (viewed|shortlisted|assessment|interview|offer|rejected)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    last_update_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    final_url: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    blocker: Mapped[str | None] = mapped_column(Text)
    answers: Mapped[list[Any]] = mapped_column(default=list)          # [{question, answer, source}]
    artifacts: Mapped[list[Any]] = mapped_column(default=list)        # screenshot / transcript paths

    job: Mapped[Job] = relationship(back_populates="applications")
    events: Mapped[list["Event"]] = relationship(back_populates="application", cascade="all, delete-orphan",
                                                 order_by="Event.at")


class Event(Base):
    """Timeline entry: an email reply, a status change, a blocker, a note."""
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    summary: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)

    application: Mapped[Application | None] = relationship(back_populates="events")


class EmailSeen(Base):
    __tablename__ = "emails_seen"

    message_id: Mapped[str] = mapped_column(String(300), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))          # alert|tracking|ignored
    subject: Mapped[str | None] = mapped_column(Text)
    sender: Mapped[str | None] = mapped_column(String(300))
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)


class SiteLogin(Base):
    """Logins the agent created on company career sites. Password is Fernet-encrypted."""
    __tablename__ = "site_logins"

    domain: Mapped[str] = mapped_column(String(200), primary_key=True)
    username: Mapped[str] = mapped_column(String(200))
    password_enc: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    note: Mapped[str | None] = mapped_column(Text)


class LLMUsage(Base):
    """One Claude call (or agent session) and what it consumed. Shared subscription, so we watch it."""
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    task: Mapped[str] = mapped_column(String(24), index=True)      # scoring|apply|email|prep|health
    model: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)   # API-price equivalent; you pay via subscription
    turns: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    ref: Mapped[str | None] = mapped_column(String(64))           # e.g. "app-12", "batch"
    ok: Mapped[bool] = mapped_column(Boolean, default=True)


class RateLimitState(Base):
    """Latest subscription-limit reading per window, from the SDK's rate_limit events."""
    __tablename__ = "rate_limits"

    limit_type: Mapped[str] = mapped_column(String(32), primary_key=True)   # five_hour|seven_day|...
    status: Mapped[str] = mapped_column(String(24))                         # allowed|allowed_warning|rejected
    utilization: Mapped[float | None] = mapped_column(Float)
    resets_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)   # discover|score|apply|email|summary
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ok: Mapped[bool | None] = mapped_column(Boolean)
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class CompanySender(Base):
    """Email domain a company you applied to writes from, learned from Gmail headers (mail.py)."""
    __tablename__ = "company_senders"

    company: Mapped[str] = mapped_column(String(200), primary_key=True)
    domain: Mapped[str | None] = mapped_column(String(200))   # None = looked, found nothing yet
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class CompanyRating(Base):
    """AmbitionBox ratings per company name as it appears in postings (see companies.py)."""
    __tablename__ = "company_ratings"

    name: Mapped[str] = mapped_column(String(200), primary_key=True)   # normalised posting company name
    overall: Mapped[int | None] = mapped_column(Integer)              # 1, 3 or 5; None = no usable reviews
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)      # per-criterion 1/3/5, raw ratings, url
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


_engine = None
_Session: sessionmaker[Session] | None = None


def engine():
    global _engine, _Session
    if _engine is None:
        config.ensure_dirs()
        _engine = create_engine(f"sqlite:///{config.DB_PATH}", future=True,
                                connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(_engine, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        Base.metadata.create_all(_engine)
        with _engine.begin() as conn:
            conn.execute(text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5("
                "path UNINDEXED, title, body, tags, tokenize='porter unicode61')"))
        _Session = sessionmaker(_engine, expire_on_commit=False, future=True)
    return _engine


@contextmanager
def session() -> Iterator[Session]:
    engine()
    assert _Session is not None
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def add_event(s: Session, kind: str, summary: str, *, application: Application | None = None,
              job_id: int | None = None, detail: dict[str, Any] | None = None) -> Event:
    ev = Event(kind=kind, summary=summary, detail=detail or {},
               application_id=application.id if application else None,
               job_id=job_id if job_id is not None else (application.job_id if application else None))
    s.add(ev)
    return ev


@contextmanager
def run_log(kind: str) -> Iterator[dict[str, Any]]:
    """Record a scheduled run with its stats; re-raises after logging the error."""
    stats: dict[str, Any] = {}
    with session() as s:
        r = Run(kind=kind)
        s.add(r)
        s.flush()
        run_id = r.id
    try:
        yield stats
    except Exception as exc:
        with session() as s:
            r = s.get(Run, run_id)
            r.finished_at, r.ok, r.error, r.stats = utcnow(), False, f"{type(exc).__name__}: {exc}", _jsonable(stats)
        raise
    else:
        with session() as s:
            r = s.get(Run, run_id)
            r.finished_at, r.ok, r.stats = utcnow(), True, _jsonable(stats)


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(d, default=str))
