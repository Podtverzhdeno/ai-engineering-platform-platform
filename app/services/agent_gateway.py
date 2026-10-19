import datetime as dt
import fnmatch

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import audit
from app.models import AgentRun, ChangeSet, Project, Task, new_id, utcnow

WORKTREE_ROOT = "/workspaces"
RUNTIME = "opencode-1.19.3-sandbox"


class Violation(HTTPException):
    def __init__(self, message: str, code: str = "SANDBOX_VIOLATION"):
        super().__init__(status_code=403, detail={"error": code, "message": message})


def run_session(db: Session, project: Project, change: ChangeSet, task: Task, model_profile: dict | None = None) -> AgentRun:
    if change.state not in ("DESIGN_APPROVED", "EXECUTING"):
        raise Violation("coding requires DESIGN_APPROVED spec", "CORE-01")
    scope_conflict = (
        db.query(Task)
        .join(ChangeSet, ChangeSet.id == Task.change_id)
        .filter(
            ChangeSet.project_id == project.id,
            Task.status.in_(("READY", "RUNNING")),
            Task.id != task.id,
        )
        .all()
    )
    for other in scope_conflict:
        for pa in task.write_scope:
            for pb in other.write_scope or []:
                if pa == pb or fnmatch.fnmatch(pb, pa) or fnmatch.fnmatch(pa, pb):
                    raise Violation(f"write scope overlaps with {other.id}", "WRITE_SCOPE_CONFLICT")

    run = AgentRun(
        id=new_id("RUN"),
        task_id=task.id,
        change_id=change.id,
        runtime=RUNTIME,
        model=(model_profile or {}).get("model", "provider-default"),
        tool_profile=(model_profile or {}).get("tool_profile", "developer-restricted"),
        status="PENDING",
        traces=[{"event": "session.created", "ts": utcnow().isoformat()}],
    )
    db.add(run)
    task.status = "RUNNING"
    audit.append(db, project.id, "agent-gateway", "agent.session_created", {"task": task.title, "run": run.id})
    _maybe_live_dispatch(run, task, change, project)
    return run


def _maybe_live_dispatch(run: AgentRun, task: Task, change: ChangeSet, project: Project) -> None:
    import os

    if not os.environ.get("AIPLATFORM_OPENCODE_LIVE"):
        return
    from app.services import opencode_runtime
    from app.services import memory as memory_svc

    contract_text = (
        f"Task: {task.title}\n"
        f"Write scope (не выходить за границы): {task.write_scope}\n"
        f"Acceptance: {task.acceptance_ids}\n"
        f"Правила: не менять файлы вне write scope; самоподтверждение запрещено; "
        f"комментарий-summary≤200 слов. Не исполняй shell вне worktree.\n"
        f"Спецификация (spec_hash={change.spec_hash}): {change.artifacts.get('delta_md', '')[:1500]}\n"
        f"Контекст проекта:\n{memory_svc.prompt_block(db, project)}"
    )
    try:
        session_id = opencode_runtime.create_session({"title": f"{project.id}:{task.title}"})
        reply = opencode_runtime.post_message(session_id, contract_text)
        excerpt = str(reply)[:500]
        run.traces = [
            *run.traces,
            {"event": "opencode.session_open", "ts": utcnow().isoformat(), "session_id": session_id},
            {"event": "opencode.reply", "ts": utcnow().isoformat(), "excerpt": excerpt},
        ]
    except HTTPException as exc:
        run.status = "MODEL_UNAVAILABLE"
        run.traces = [
            *run.traces,
            {"event": "opencode.error", "ts": utcnow().isoformat(), "detail": str(exc.detail)[:300]},
        ]


def finish_session(db: Session, run: AgentRun, outputs: dict, project: Project) -> dict:
    import re

    required = ["commit_sha", "test_result", "diff_summary", "open_questions"]
    missing_keys = [k for k in required if k not in outputs]
    invalid_sha = []
    sha = outputs.get("commit_sha", "")
    if sha and not re.fullmatch(r"[0-9a-f]{40}", str(sha)):
        invalid_sha = ["commit_sha format must be 40 hex chars"]
    missing_keys = [k for k in missing_keys if k != "commit_sha"] + (["commit_sha"] if not sha else [])
    if missing_keys or invalid_sha:
        run.status = "FAILED"
        raise HTTPException(
            status_code=422,
            detail={"error": "OUTPUT_INCOMPLETE", "missing": missing_keys + invalid_sha, "contract": "E.2"},
        )
    run.result = outputs
    run.status = "DONE"
    run.traces = [
        *run.traces,
        {"event": "session.completed", "ts": utcnow().isoformat(), "commit_sha": outputs.get("commit_sha")},
    ]
    audit.append(db, project.id, "agent-gateway", "agent.session_finished", {"run": run.id, "commit": outputs.get("commit_sha")})
    return {"run": run.id, "task_id": run.task_id, "status": run.status}


def cancel_session(db: Session, run: AgentRun, project: Project) -> dict:
    run.status = "CANCELLED"
    run.traces = [*run.traces, {"event": "session.cancelled", "ts": utcnow().isoformat()}]
    task = db.get(Task, run.task_id)
    if task is not None:
        task.status = "PENDING"
    audit.append(db, project.id, "agent-gateway", "agent.session_cancelled", {"run": run.id})
    return {"run": run.id, "status": run.status}


def feature_available(name: str) -> bool:
    return os_feature_flag(name)


def os_feature_flag(name: str) -> bool:
    import os

    enabled = {"opencode_real_session": "AIPLATFORM_OPENCODE_LIVE"}
    return bool(os.environ.get(enabled.get(name, "")))
