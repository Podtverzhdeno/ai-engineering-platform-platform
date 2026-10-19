from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import current_user
from app.api import changes as changes_api
from app.db import get_db
from app.gates import product_spec_hash
from app.models import Approval, ChangeSet, new_id, Project, User
from app.policy import require_member
from app.services import state_machine

router = APIRouter()


class ApprovalIn(BaseModel):
    project_id: str
    purpose: str
    change_id: str | None = None
    spec_hash: str | None = None
    commit_sha: str = ""


ALLOWED_PURPOSES = {"product", "design", "coding", "release", "migration"}


def _apply_committed_effect(db: Session, approval: Approval, project: Project) -> dict:
    notes = []
    if approval.purpose == "product" and project.state == "PRODUCT_PROPOSED":
        if approval.spec_hash == project.product_hash:
            state_machine.transition(db, project, "PRODUCT_APPROVED")

    elif approval.purpose == "design":
        change = db.get(ChangeSet, approval.change_id) if approval.change_id else None
        if change is None:
            raise HTTPException(status_code=422, detail="design approval requires change_id")
        if change.state not in ("PROPOSED", "DESIGNING"):
            raise HTTPException(status_code=409, detail={"error": "INVALID_CHANGE_STATE", "state": change.state})
        risk = _risk_from_change(change)
        required = changes_api.risk_gate_requirements(risk)
        if required["threat_model_required"] and not change.artifacts.get("threat_model_md"):
            change.state = "DESIGNING"
            db.flush()
            notes.append("DESIGN_GATE_BLOCKED: threat model required for HIGH/CRITICAL risk (EPIC-07)")
            return {"blocked": notes, "change_state": change.state}
        approvals_count = (
            db.query(Approval)
            .filter(
                Approval.project_id == project.id,
                Approval.purpose == "design",
                Approval.change_id == change.id,
                Approval.spec_hash == change.spec_hash,
            )
            .count()
        )
        if approvals_count < required["min_approvals"]:
            change.state = "DESIGNING"
            db.flush()
            notes.append(f"MIN_APPROVALS_NOT_MET: have {approvals_count}, need {required['min_approvals']}")
            return {"blocked": notes, "change_state": change.state}
        change.state = "DESIGN_APPROVED"
        if project.state == "PRODUCT_APPROVED":
            state_machine.transition(db, project, "DESIGN_PROPOSED")
            state_machine.transition(db, project, "DESIGN_APPROVED")
        db.flush()
        audit.append(db, project.id, approval.actor_id, "design.approved", {"change_id": change.id})
    return {"blocked": notes, "change_state": None}


def _risk_from_change(change: ChangeSet) -> str:
    from app.policy import risk_floor

    return risk_floor(list(change.affected_paths or []), list(change.scope or [] ))


@router.post("/v1/approvals", status_code=201)
def create_approval(
    payload: ApprovalIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = db.get(Project, payload.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    require_member(db, project, user.id, min_role="owner")
    if payload.purpose not in ALLOWED_PURPOSES:
        raise HTTPException(status_code=422, detail="invalid purpose")

    bound_hash = payload.spec_hash
    if payload.purpose == "product":
        bound_hash = bound_hash or project.product_hash or product_spec_hash(project)
    elif payload.purpose == "design" and payload.change_id:
        change = db.get(ChangeSet, payload.change_id)
        if change is not None and change.spec_hash:
            bound_hash = bound_hash or change.spec_hash
    else:
        bound_hash = bound_hash or product_spec_hash(project)

    approval = Approval(
        id=new_id("APR"),
        project_id=project.id,
        actor_id=user.id,
        purpose=payload.purpose,
        change_id=payload.change_id or "",
        spec_hash=bound_hash or "",
        commit_sha=payload.commit_sha,
    )
    db.add(approval)
    db.flush()
    audit.append(
        db,
        project.id,
        user.id,
        "approval.created",
        {"approval_id": approval.id, "purpose": payload.purpose, "spec_hash": bound_hash or ""},
    )
    _apply_committed_effect(db, approval, project)
    db.commit()
    result = {
        "id": approval.id,
        "purpose": approval.purpose,
        "spec_hash": approval.spec_hash,
        "status": approval.status,
        "project_state": project.state,
    }
    if approval.change_id:
        change = db.get(ChangeSet, approval.change_id)
        if change is not None:
            result["change_state"] = change.state
    return result


@router.get("/v1/projects/{project_id}/approvals")
def list_approvals(
    project_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    require_member(db, project, user.id, min_role="viewer")
    approvals = db.query(Approval).filter(Approval.project_id == project.id).order_by(Approval.created_at).all()
    return [
        {"id": a.id, "purpose": a.purpose, "change_id": a.change_id, "spec_hash": a.spec_hash, "status": a.status, "actor": a.actor_id}
        for a in approvals
    ]
