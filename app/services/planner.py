import fnmatch
import os
import re

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import audit
from app.models import ChangeSet, Project, Task, new_id

LIMITS_DEFAULT = {"max_runtime_minutes": 50, "max_attempts": 3}


class ScopeConflict(HTTPException):
    def __init__(self, conflicts: list[tuple[str, str]]):
        super().__init__(
            status_code=422,
            detail={"error": "WRITE_SCOPE_CONFLICT", "conflicts": [list(x) for x in conflicts]},
        )


def _scopes_overlap(a: list[str], b: list[str]) -> bool:
    for pa in a:
        for pb in b:
            if pa == pb or fnmatch.fnmatch(pb, pa) or fnmatch.fnmatch(pa, pb):
                return True
    return False


def plan_change(db: Session, project: Project, change: ChangeSet, tasks: list[dict], actor: str) -> dict:
    if change.state != "DESIGN_APPROVED":
        raise HTTPException(status_code=403, detail={"error": "PLAN_BLOCKED", "state": change.state})
    keys = [t["key"] for t in tasks]
    if len(keys) != len(set(keys)):
        raise HTTPException(status_code=422, detail="duplicate task keys")
    key_set = set(keys)
    conflicts = []
    rows = []
    for t in tasks:
        for dep in t.get("depends_on", []):
            if dep not in key_set:
                raise HTTPException(status_code=422, detail={"error": "UNKNOWN_DEPENDENCY", "dep": dep})
        if not t.get("acceptance_ids"):
            raise HTTPException(status_code=422, detail={"error": "ACCEPTANCE_REQUIRED", "task": t["key"]})
    for i, t0 in enumerate(tasks):
        for t1 in tasks[i + 1:]:
            if _scopes_overlap(t0.get("write_scope", []), t1.get("write_scope", [])):
                conflicts.append((t0["key"], t1["key"]))
    if conflicts:
        raise ScopeConflict(conflicts)

    for t in tasks:
        row = Task(
            id=new_id("TASK"),
            change_id=change.id,
            title=t["key"],
            dep_ids=list(t.get("depends_on", [])),
            write_scope=list(t.get("write_scope", [])),
            read_scope=list(t.get("read_scope", ["**"])),
            acceptance_ids=list(t.get("acceptance_ids", [])),
            agent=t.get("agent", "developer"),
            limits={**LIMITS_DEFAULT, **(t.get("limits", {}))},
        )
        rows.append(row)
        db.add(row)
    db.flush()
    concurrency = ready_count(db, change)
    audit.append(
        db,
        project.id,
        actor,
        "change.planned",
        {"change_id": change.id, "tasks": len(rows), "concurrency": concurrency},
    )
    return {"tasks": [t.title for t in rows], "concurrency": concurrency}


def ready_count(db: Session, change: ChangeSet) -> int:
    tasks = db.query(Task).filter(Task.change_id == change.id).all()
    marked = _mark_ready(tasks)
    db.flush()
    return len([t for t in tasks if t.status == "READY"])


def _mark_ready(tasks: list[Task]) -> int:
    by_id = {t.title: t for t in tasks}
    count = 0
    for task in tasks:
        if task.status != "PENDING":
            continue
        deps = by_id.get(task.title)
        dep_states = [by_id.get(d, None) for d in task.dep_ids]
        ready = all(d is not None and d.status == "DONE" for d in dep_states)
        task.status = "READY" if ready else "PENDING"
        count += 1 if ready else 0
    return count


def complete_task(db: Session, task: Task, project: Project, outputs: dict, actor: str = "agent") -> dict:
    task.outputs = outputs
    task.status = "DONE"
    change = db.get(ChangeSet, task.change_id)
    db.flush()
    effect = ready_count(db, change)
    all_done = all(t.status == "DONE" for t in db.query(Task).filter(Task.change_id == change.id).all())
    result = {"task": task.title, "status": "DONE", "now_ready": effect, "all_done": all_done}
    if all_done:
        from app.services import state_machine

        if project.state == "IMPLEMENTATION_ALLOWED":
            state_machine.transition(db, project, "ALL_TASKS_DONE")
        result["project_state"] = project.state
    audit.append(db, project.id, actor, "task.completed", {"task": task.title, "outputs_keys": list(outputs)})
    return result
