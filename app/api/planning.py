from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ChangeSet, Project, Task, User
from app.policy import require_member
from app.services import planner
from app.api.deps import current_user

router = APIRouter()


class PlanIn(BaseModel):
    tasks: list[dict]


class AgentRunIn(BaseModel):
    task_key: str


class TaskResultIn(BaseModel):
    commit_sha: str = "5f1a3b9c7d2e4f6081a2b3c4d5e6f7089abcdef01"
    test_result: str = "PASS"
    diff_summary: str = "implemented change"
    open_questions: list[str] = []


def _project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _change(db: Session, change_id: str) -> ChangeSet:
    change = db.get(ChangeSet, change_id)
    if change is None:
        raise HTTPException(status_code=404, detail="change not found")
    return change


@router.post("/v1/changes/{change_id}/plan")
def plan(
    change_id: str,
    payload: PlanIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    change = _change(db, change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="owner")
    result = planner.plan_change(db, project, change, payload.tasks, actor=user.id)
    db.commit()
    return result


@router.get("/v1/changes/{change_id}/tasks")
def list_tasks(
    change_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    change = _change(db, change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="viewer")
    planner._mark_ready(db.query(Task).filter(Task.change_id == change.id).all())
    db.commit()
    return [
        {
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "deps": t.dep_ids,
            "write_scope": t.write_scope,
            "acceptance_ids": t.acceptance_ids,
        }
        for t in db.query(Task).filter(Task.change_id == change.id).all()
    ]


@router.get("/v1/runs/{run_id}")
def read_run(
    run_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    from app.models import AgentRun

    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    task = db.get(Task, run.task_id)
    change = _change(db, run.change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="viewer")
    workspace_path = next(
        (t.get("path") for t in reversed(run.traces) if isinstance(t, dict) and t.get("path")),
        None,
    )
    return {
        "run_id": run.id,
        "status": run.status,
        "task": {
            "id": task.id if task else None,
            "title": task.title if task else None,
            "write_scope": task.write_scope if task else [],
            "acceptance_ids": task.acceptance_ids if task else [],
            "outputs": task.outputs if task else {},
        },
        "traces": run.traces,
        "workspace": workspace_path,
        "result": run.result,
    }


@router.post("/v1/changes/{change_id}/tasks/{task_key}/run", status_code=201)
def run_task(
    change_id: str,
    task_key: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    change = _change(db, change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="member")
    task = (
        db.query(Task)
        .filter(Task.change_id == change.id, Task.title == task_key)
        .one_or_none()
    )
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status != "READY":
        raise HTTPException(status_code=409, detail={"error": "TASK_NOT_READY", "state": task.status})

    if project.budget_usd and project.spent_usd >= project.budget_usd:
        raise HTTPException(
            status_code=402,
            detail={"error": "BUDGET_EXCEEDED", "budget_usd": project.budget_usd, "spent_usd": project.spent_usd},
        )
    from app.services import agent_gateway, workspace

    run = agent_gateway.run_session(db, project, change, task)
    try:
        workspace.materialize(db, project, change, task, spec_hash=change.spec_hash)
        run.traces = [*run.traces, {"event": "workspace.materialized", "path": workspace.root() + "/" + task.id}]
    except OSError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail={"error": "WORKSPACE_UNAVAILABLE", "message": str(exc)}) from exc
    project.spent_usd += 1.0
    db.commit()
    return {"run_id": run.id, "task": task.title, "status": run.status}


@router.post("/v1/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    from app.models import AgentRun
    from app.services import agent_gateway

    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status in ("DONE", "CANCELLED"):
        raise HTTPException(status_code=409, detail={"error": "INVALID_TRANSITION", "state": run.status})
    task = db.get(Task, run.task_id)
    if task is not None and task.status == "RUNNING":
        task.status = "PENDING"
    change = _change(db, run.change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="member")
    result = agent_gateway.cancel_session(db, run, project)
    planner.ready_count(db, change)
    db.commit()
    return result


@router.post("/v1/runs/{run_id}/finish")
def finish_run(
    run_id: str,
    payload: TaskResultIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    from app.models import AgentRun
    from app.services import agent_gateway

    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    change = _change(db, run.change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="member")
    task = None
    if run.task_id:
        task = db.get(Task, run.task_id)
        if task is not None and task.status == "RUNNING":
            task.status = "PENDING"
    result = agent_gateway.finish_session(db, run, {
        "commit_sha": payload.commit_sha,
        "test_result": payload.test_result,
        "diff_summary": payload.diff_summary,
        "open_questions": payload.open_questions,
    }, project)
    outputs = {"commit_sha": payload.commit_sha, "test_result": payload.test_result}
    if task is not None:
        task.status = "DONE"
        task.outputs = outputs
        planner.ready_count(db, change)
    try:
        from app.services import workspace as ws_mod

        ws_mod.cleanup(ws_mod.root() + ("/" + run.task_id))
    except OSError:
        pass
    db.commit()
    return result
