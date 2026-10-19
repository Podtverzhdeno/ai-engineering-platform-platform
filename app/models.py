import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    owner_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    classification: Mapped[str] = mapped_column(String(16), default="PRIVATE")
    mode: Mapped[str] = mapped_column(String(16), default="NEW")
    repo_refs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(32), default="CREATED")
    spec_version: Mapped[int] = mapped_column(Integer, default=1)
    product_hash: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    budget_usd: Mapped[float] = mapped_column(default=0.0)
    spent_usd: Mapped[float] = mapped_column(default=0.0)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))


ROLE_RANK = {"viewer": 1, "member": 2, "owner": 3}


class InterviewSession(Base):
    __tablename__ = "interview_sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    questions: Mapped[list] = mapped_column(JSON, default=list)
    answers: Mapped[list] = mapped_column(JSON, default=list)
    gaps: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="IN_PROGRESS")


class Requirement(Base):
    __tablename__ = "requirements"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    domain: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(String(2000))
    scenarios: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="PROPOSED")
    source: Mapped[str] = mapped_column(String(32), default="USER_EXPLICIT")
    revision: Mapped[int] = mapped_column(Integer, default=1)


class ChangeSet(Base):
    __tablename__ = "change_sets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    scope: Mapped[list] = mapped_column(JSON, default=list)
    affected_paths: Mapped[list] = mapped_column(JSON, default=list)
    baseline_sha: Mapped[str] = mapped_column(String(80), default="")
    spec_hash: Mapped[str] = mapped_column(String(80), default="")
    artifacts: Mapped[dict] = mapped_column(JSON, default=dict)
    review_cycles: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(32), default="PROPOSED")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ArchitectureDecision(Base):
    __tablename__ = "adrs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    decision: Mapped[str] = mapped_column(String(2000))
    alternatives: Mapped[list] = mapped_column(JSON, default=list)
    tradeoffs: Mapped[list] = mapped_column(JSON, default=list)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    change_id: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    dep_ids: Mapped[list] = mapped_column(JSON, default=list)
    write_scope: Mapped[list] = mapped_column(JSON, default=list)
    read_scope: Mapped[list] = mapped_column(JSON, default=list)
    acceptance_ids: Mapped[list] = mapped_column(JSON, default=list)
    limits: Mapped[dict] = mapped_column(JSON, default=dict)
    outputs: Mapped[dict] = mapped_column(JSON, default=dict)
    agent: Mapped[str] = mapped_column(String(64), default="developer")
    status: Mapped[str] = mapped_column(String(32), default="PENDING")


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), index=True)
    change_id: Mapped[str] = mapped_column(String(32), index=True, default="")
    runtime: Mapped[str] = mapped_column(String(64), default="opencode")
    model: Mapped[str] = mapped_column(String(128), default="")
    tool_profile: Mapped[str] = mapped_column(String(64), default="developer-restricted")
    traces: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")


class Finding(Base):
    __tablename__ = "findings"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    change_id: Mapped[str] = mapped_column(String(32), index=True)
    classification: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    source_agent: Mapped[str] = mapped_column(String(64), default="")
    file: Mapped[str] = mapped_column(String(300), default="")
    line_range: Mapped[list] = mapped_column(JSON, default=list)
    scenario: Mapped[str] = mapped_column(String(1000), default="")
    expected: Mapped[str] = mapped_column(String(1000), default="")
    actual: Mapped[str] = mapped_column(String(1000), default="")
    reproduction: Mapped[str] = mapped_column(String(1000), default="")
    evidence_uri: Mapped[str] = mapped_column(String(300), default="")
    verifier_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    verifier_state: Mapped[str] = mapped_column(String(32), default="OPEN")
    resolution: Mapped[str] = mapped_column(String(32), default="OPEN")


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    actor_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"))
    purpose: Mapped[str] = mapped_column(String(64))
    change_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    spec_hash: Mapped[str] = mapped_column(String(80), index=True)
    commit_sha: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    artifact_hash: Mapped[str] = mapped_column(String(80), default="")
    links: Mapped[list] = mapped_column(JSON, default=list)
    verifier: Mapped[str] = mapped_column(String(64), default="")
    retention_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Release(Base):
    __tablename__ = "releases"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    change_id: Mapped[str] = mapped_column(String(32), index=True, default="")
    commit_sha: Mapped[str] = mapped_column(String(80), default="")
    artifacts: Mapped[list] = mapped_column(JSON, default=list)
    flag_plan: Mapped[dict] = mapped_column(JSON, default=dict)
    approvals: Mapped[list] = mapped_column(JSON, default=list)
    environment: Mapped[str] = mapped_column(String(32), default="staging")
    state: Mapped[str] = mapped_column(String(32), default="DRAFT")


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    impact: Mapped[str] = mapped_column(String(2000), default="")
    traces: Mapped[list] = mapped_column(JSON, default=list)
    rca: Mapped[str] = mapped_column(String(4000), default="")
    fix: Mapped[dict] = mapped_column(JSON, default=dict)
    regression_case: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(32), default="OPEN")


class EvalReport(Base):
    __tablename__ = "eval_reports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    dataset_version: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64), default="")
    results: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    row_hash: Mapped[str] = mapped_column(String(64))
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ts_text: Mapped[str] = mapped_column(String(64))


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="RUNNABLE")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(String(2000), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
