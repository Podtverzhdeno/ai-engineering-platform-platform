import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import current_user
from app.db import get_db
from app.gates import check_coding_gate, product_spec_hash
from app.models import AgentRun, new_id, Project, User
from app.policy import require_member
from app.services import workflow

router = APIRouter()


def _start_opencode_session(db: Session, job) -> None:
    calls = int(job.state.get("external_calls", 0)) + 1
    job.state = {
        "external_calls": calls,
        "session": {"server": "opencode://sandbox-runner", "start": dt.datetime.now(dt.timezone.utc).isoformat()},
    }
    db.flush()


workflow.register("opencode.start-session", _start_opencode_session)


class CodingIn(BaseModel):
    project_id: str
    task: str = "Implement approved API contract"


@router.post("/v1/coding-sessions", status_code=202)
def request_coding_session(
    payload: CodingIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = db.get(Project, payload.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    require_member(db, project, user.id, min_role="member")
    approval = check_coding_gate(db, project)
    run = AgentRun(id=new_id("RUN"), task_id=new_id("TASK"), runtime="opencode", status="PENDING")
    db.add(run)
    db.flush()
    job, created = workflow.enqueue(
        db,
        "opencode.start-session",
        {"run_id": run.id, "task": payload.task},
        idempotency_key=f"coding:{run.id}",
    )
    audit.append(
        db,
        project.id,
        user.id,
        "coding.session_requested",
        {"run_id": run.id, "job_id": job.id, "approval_id": approval.id, "spec_hash": approval.spec_hash},
    )
    db.commit()
    return {"run_id": run.id, "job_id": job.id, "queued_new": created, "state": project.state, "spec_hash": approval.spec_hash}
