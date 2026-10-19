from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import current_user
from app.db import get_db
from app.gates import invalidate_stale_approvals
from app.models import AuditEvent, Membership, new_id, Project, User
from app.policy import ALLOWED_CLASSIFICATION, ALLOWED_MODE, require_member

router = APIRouter()


class UserIn(BaseModel):
    name: str


class ProjectIn(BaseModel):
    name: str
    mode: str = "NEW"
    classification: str = "PRIVATE"


class MemberIn(BaseModel):
    user_id: str
    role: str = "member"


class RevisionIn(BaseModel):
    reason: str


class BudgetIn(BaseModel):
    budget_usd: float


@router.post("/v1/users", status_code=201)
def create_user(payload: UserIn, db: Session = Depends(get_db)) -> dict:
    existing = db.query(User).filter(User.name == payload.name).first()
    if existing is not None:
        return JSONResponse(status_code=200, content={"id": existing.id, "name": existing.name})
    user = User(id=new_id("usr"), name=payload.name)
    db.add(user)
    db.commit()
    return {"id": user.id, "name": user.name}


@router.post("/v1/projects", status_code=201)
def create_project(
    payload: ProjectIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    if payload.mode not in ALLOWED_MODE:
        raise HTTPException(status_code=422, detail="invalid mode")
    if payload.classification not in ALLOWED_CLASSIFICATION:
        raise HTTPException(status_code=422, detail="invalid classification")
    project = Project(
        id=new_id("PRJ"),
        name=payload.name,
        owner_id=user.id,
        mode=payload.mode,
        classification=payload.classification,
    )
    db.add(project)
    db.flush()  # project row must exist before membership FK (pass on real RDBMS, not only SQLite)
    db.add(
        Membership(
            id=new_id("mem"),
            project_id=project.id,
            user_id=user.id,
            role="owner",
        )
    )
    db.flush()
    audit.append(db, project.id, user.id, "project.created", {"mode": payload.mode, "classification": payload.classification})
    db.commit()
    return {"id": project.id, "state": project.state, "spec_version": project.spec_version}


@router.get("/v1/projects")
def list_projects(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    memberships = db.query(Membership).filter(Membership.user_id == user.id).all()
    ids = [m.project_id for m in memberships]
    projects = db.query(Project).filter(Project.id.in_(ids)).all() if ids else []
    return [{"id": p.id, "name": p.name, "state": p.state, "mode": p.mode, "classification": p.classification} for p in projects]


@router.get("/v1/projects/{project_id}")
def get_project(
    project_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    require_member(db, project, user.id, min_role="viewer")
    return {
        "id": project.id,
        "name": project.name,
        "state": project.state,
        "mode": project.mode,
        "classification": project.classification,
        "spec_version": project.spec_version,
        "owner_id": project.owner_id,
    }


@router.post("/v1/projects/{project_id}/members", status_code=201)
def add_member(
    project_id: str,
    payload: MemberIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    require_member(db, project, user.id, min_role="owner")
    target = db.get(User, payload.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if payload.role not in ("viewer", "member", "owner"):
        raise HTTPException(status_code=422, detail="invalid role")
    db.add(Membership(id=new_id("mem"), project_id=project.id, user_id=target.id, role=payload.role))
    db.flush()
    audit.append(db, project.id, user.id, "membership.added", {"user": target.id, "role": payload.role})
    db.commit()
    return {"project_id": project.id, "user_id": target.id, "role": payload.role}


@router.post("/v1/projects/{project_id}/revision")
def bump_revision(
    project_id: str,
    payload: RevisionIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    require_member(db, project, user.id, min_role="owner")
    project.spec_version += 1
    stale = invalidate_stale_approvals(db, project)
    db.flush()
    audit.append(
        db,
        project.id,
        user.id,
        "spec.revision_bumped",
        {"reason": payload.reason, "spec_version": project.spec_version, "invalidated_approvals": stale},
    )
    db.commit()
    return {"spec_version": project.spec_version, "invalidated_approvals": stale}


@router.post("/v1/projects/{project_id}/budget")
def set_budget(
    project_id: str,
    payload: BudgetIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    require_member(db, project, user.id, min_role="owner")
    if payload.budget_usd < 0:
        raise HTTPException(status_code=422, detail="budget must be >= 0")
    project.budget_usd = payload.budget_usd
    audit.append(db, project.id, user.id, "project.budget_set", {"budget_usd": payload.budget_usd})
    db.commit()
    return {"budget_usd": project.budget_usd, "spent_usd": project.spent_usd}


@router.get("/v1/projects/{project_id}/audit")
def read_audit(
    project_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    require_member(db, project, user.id, min_role="owner")
    events = db.query(AuditEvent).filter(AuditEvent.project_id == project.id).order_by(AuditEvent.id).all()
    ok, broken = audit.verify(db, project.id)
    return {
        "chain_ok": ok,
        "broken_event_id": broken,
        "events": [
            {"id": e.id, "action": e.action, "actor": e.actor_id, "payload": e.payload, "ts": e.ts_text}
            for e in events
        ],
    }
