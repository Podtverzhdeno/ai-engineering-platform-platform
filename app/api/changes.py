from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import current_user
from app.db import get_db
from app.models import ChangeSet, new_id, Project, User
from app.policy import require_member
from app.services import spec_registry, tech_advisor, workflow

router = APIRouter()


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


class ChangeIn(BaseModel):
    project_id: str
    title: str
    scope_types: list[str] = []
    affected_paths: list[str] = []


class DeltaIn(BaseModel):
    delta_md: str


class DesignIn(BaseModel):
    design_md: str
    threat_model_md: str = ""
    security_md: str = ""
    test_plan_md: str = ""
    operations_md: str = ""
    acceptance_coverage: dict = {}


class AdvisorIn(BaseModel):
    layer: str
    constraints: list[str] = []
    nfr: dict = {}


class ADRIn(BaseModel):
    adr_id: str
    chosen: str


@router.post("/v1/changes", status_code=201)
def create_change(
    payload: ChangeIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = _project(db, payload.project_id)
    require_member(db, project, user.id, min_role="owner")
    if project.state not in ("PRODUCT_APPROVED", "DESIGN_APPROVED"):
        raise HTTPException(
            status_code=403,
            detail={"error": "PRODUCT_NOT_APPROVED", "required_event": "PRODUCT_APPROVED", "state": project.state},
        )
    change = ChangeSet(
        id=new_id("CHG"),
        project_id=project.id,
        title=payload.title,
        scope=payload.scope_types,
        affected_paths=payload.affected_paths,
    )
    db.add(change)
    db.flush()
    audit.append(
        db,
        project.id,
        user.id,
        "change.created",
        {"change_id": change.id, "title": payload.title, "paths": payload.affected_paths},
    )
    db.commit()
    return {"id": change.id, "state": "PROPOSED"}


@router.get("/v1/changes/{change_id}")
def get_change(
    change_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    change = _change(db, change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="viewer")
    return {
        "id": change.id,
        "title": change.title,
        "state": change.state,
        "spec_hash": change.spec_hash,
        "artifacts": change.artifacts,
        "affected_paths": change.affected_paths,
        "review_cycles": change.review_cycles,
    }


@router.post("/v1/changes/{change_id}/delta")
def propose_delta(
    change_id: str,
    payload: DeltaIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    change = _change(db, change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="owner")
    try:
        delta = spec_registry.parse_delta(payload.delta_md)
    except spec_registry.SpecValidationError as exc:
        raise exc
    change.artifacts = {**change.artifacts, "delta": delta}
    change.spec_hash = spec_registry.spec_hash(delta)
    db.flush()
    audit.append(db, project.id, user.id, "change.delta_proposed", {"change_id": change.id, "spec_hash": change.spec_hash})
    db.commit()
    return {"spec_hash": change.spec_hash, "delta": delta}


@router.post("/v1/changes/{change_id}/design")
def propose_design(
    change_id: str,
    payload: DesignIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    change = _change(db, change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="owner")
    if project.state != "PRODUCT_APPROVED":
        raise HTTPException(status_code=409, detail={"error": "PRODUCT_NOT_APPROVED", "state": project.state})
    missing = [a for a in ("design_md",) if not getattr(payload, a)]
    if missing:
        raise HTTPException(status_code=422, detail={"error": "DESIGN_INCOMPLETE", "missing": missing})
    risk = _risk_from_paths(change)
    change.artifacts = {
        **change.artifacts,
        "design_md": payload.design_md,
        "security_md": payload.security_md,
        "threat_model_md": payload.threat_model_md,
        "test_plan_md": payload.test_plan_md,
        "operations_md": payload.operations_md,
        "acceptance_coverage": payload.acceptance_coverage,
    }
    change.state = "DESIGNING"
    db.flush()
    state_machine_transition_design_proposed(db, project)
    audit.append(db, project.id, user.id, "design.proposed", {"change_id": change.id, "risk": risk})
    db.commit()
    return {"state": change.state, "risk": risk, "required_for_design_gate": risk_gate_requirements(risk)}


def _risk_from_paths(change: ChangeSet) -> str:
    from app.policy import risk_floor

    ct = [p for p in (change.scope or [])]
    return risk_floor(list(change.affected_paths or []), ct)


def _min_approvals(value: str) -> int:
    return 0 if value == "LOW" else (1 if value == "MEDIUM" else 2)


def risk_gate_requirements(risk: str) -> dict:
    return {
        "design_review": True,
        "min_approvals": _min_approvals(risk),
        "threat_model_required": risk in ("HIGH", "CRITICAL"),
    }


def state_machine_transition_design_proposed(db: Session, project: Project) -> None:
    from app.services import state_machine

    if project.state == "PRODUCT_APPROVED":
        state_machine.transition(db, project, "DESIGN_PROPOSED")


@router.post("/v1/changes/{change_id}/execute", status_code=202)
def execute_change(
    change_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    change = _change(db, change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="member")
    if change.state != "DESIGN_APPROVED":
        raise HTTPException(
            status_code=403,
            detail={"error": "DESIGN_GATE_BLOCKED", "state": change.state, "missing": "design approval (CORE-01)"},
        )
    run = _register_run(db, project, change)
    job, created = workflow.enqueue(
        db,
        "opencode.execute-change",
        {"change_id": change.id, "run_id": run.id},
        idempotency_key=f"change-execute:{change.id}",
    )
    audit.append(db, project.id, user.id, "change.execute_requested", {"change_id": change.id, "run_id": run.id})
    db.commit()
    return {"run_id": run.id, "job_id": job.id, "queued_new": created, "change_state": change.state}


def _register_run(db: Session, project: Project, change: ChangeSet):
    from app.models import AgentRun

    run = AgentRun(
        id=new_id("RUN"),
        task_id=new_id("TASK"),
        change_id=change.id,
        runtime="opencode",
        status="PENDING",
    )
    db.add(run)
    db.flush()
    return run


def _execute_change_job(db: Session, job) -> None:
    calls = int(job.state.get("external_calls", 0)) + 1
    job.state = {
        "external_calls": calls,
        "session": {"server": "opencode://sandbox-runner", "kind": "change-execution"},
    }
    db.flush()


workflow.register("opencode.execute-change", _execute_change_job)


@router.post("/v1/changes/{change_id}/tech-advisor")
def run_advisor(
    change_id: str,
    payload: AdvisorIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    change = _change(db, change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="owner")
    rec = tech_advisor.recommend(db, project, change, payload.layer, payload.constraints, payload.nfr)
    db.commit()
    return rec


@router.post("/v1/changes/{change_id}/adr-approve")
def approve_adr(
    change_id: str,
    payload: ADRIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    change = _change(db, change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="owner")
    result = tech_advisor.confirm(db, project, payload.adr_id, payload.chosen)
    db.commit()
    return result
