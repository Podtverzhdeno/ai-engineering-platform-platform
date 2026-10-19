from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import current_user
from app.db import get_db
from app.models import ChangeSet, Finding, Incident, new_id, Project, Requirement, Release, User
from app.policy import require_member

router = APIRouter()


class IncidentIn(BaseModel):
    project_id: str
    impact: str
    traces: list = []


class RCAIn(BaseModel):
    rca: str
    regression_case: dict


def _project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@router.post("/v1/incidents", status_code=201)
def create_incident(
    payload: IncidentIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = _project(db, payload.project_id)
    require_member(db, project, user.id, min_role="member")
    incident = Incident(
        id=new_id("INC"),
        project_id=project.id,
        impact=payload.impact,
        traces=payload.traces,
    )
    db.add(incident)
    db.flush()
    audit.append(db, project.id, user.id, "incident.created", {"incident": incident.id})
    db.commit()
    return {"id": incident.id, "state": incident.state}


@router.post("/v1/incidents/{incident_id}/rca")
def close_with_rca(
    incident_id: str,
    payload: RCAIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    project = _project(db, incident.project_id)
    require_member(db, project, user.id, min_role="owner")
    incident.rca = payload.rca
    incident.regression_case = payload.regression_case
    incident.state = "RCA_DONE"
    db.flush()

    regression = Requirement(
        id=new_id("REQ"),
        project_id=project.id,
        domain="resilience",
        text=f"Regression for {incident.id}: {payload.rca[:200]}",
        scenarios=[payload.regression_case.get("scenario", "incident-repro")],
        status="CONFIRMED",
        source="INCIDENT_EXPLICIT",
    )
    db.add(regression)
    db.flush()
    audit.append(
        db,
        project.id,
        user.id,
        "incident.rca_recorded",
        {"incident": incident.id, "regression_req": regression.id},
    )
    db.commit()
    return {"id": incident.id, "state": incident.state, "regression_requirement": regression.id}


@router.get("/v1/incidents/{incident_id}")
def get_incident(
    incident_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    project = _project(db, incident.project_id)
    require_member(db, project, user.id, min_role="viewer")
    return {
        "id": incident.id,
        "state": incident.state,
        "impact": incident.impact,
        "rca": incident.rca,
        "regression_case": incident.regression_case,
    }


@router.get("/v1/pslc-dashboard/{project_id}")
def dashboard(
    project_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = _project(db, project_id)
    require_member(db, project, user.id, min_role="viewer")
    open_findings = (
        db.query(Finding)
        .filter(Finding.change_id.in_([c.id for c in db.query(ChangeSet).filter(ChangeSet.project_id == project.id)]))
        .filter(Finding.verifier_state == "CONFIRMED")
        .count()
        if True
        else 0
    )
    releases = db.query(Release).filter(Release.project_id == project.id).count()
    incidents = db.query(Incident).filter(Incident.project_id == project.id).count()
    denials = sum(1 for e in audit_events(db, project.id) if e["action"].startswith("policy."))
    return {
        "releases": releases,
        "incidents": incidents,
        "confirmed_open_findings": open_findings,
        "policy_denials": denials,
        "budget_usd": project.budget_usd,
        "spent_usd": project.spent_usd,
    }


def audit_events(db: Session, project_id: str) -> list[dict]:
    from app.models import AuditEvent

    events = db.query(AuditEvent).filter(AuditEvent.project_id == project_id).all()
    return [{"action": e.action} for e in events]


@router.get("/v1/analytics/{project_id}")
def product_analytics(
    project_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    import statistics

    from app.models import AgentRun, Finding, Task

    project = _project(db, project_id)
    require_member(db, project, user.id, min_role="viewer")
    tasks = (
        db.query(Task)
        .filter(Task.change_id.in_([c.id for c in db.query(ChangeSet).filter(ChangeSet.project_id == project.id)]))
        .all()
    )
    run_states = [
        r.status
        for r in db.query(AgentRun)
        .filter(AgentRun.change_id.in_([c.id for c in db.query(ChangeSet).filter(ChangeSet.project_id == project.id)]))
        .all()
    ]
    changes = db.query(ChangeSet).filter(ChangeSet.project_id == project.id).all()
    findings = (
        db.query(Finding)
        .filter(Finding.change_id.in_([c.id for c in changes]))
        .all()
    )
    verdicts: dict[str, int] = {}
    for finding in findings:
        verdicts[finding.verifier_state] = verdicts.get(finding.verifier_state, 0) + 1
    return {
        "project": project.id,
        "tasks_total": len(tasks),
        "tasks_delivered": sum(1 for state in run_states if state == "DONE"),
        "review_cycles_avg": round(statistics.mean([c.review_cycles for c in changes]), 2) if changes else 0.0,
        "findings_by_verdict": verdicts,
        "incidents_open": db.query(Incident).filter(Incident.project_id == project.id, Incident.state == "OPEN").count(),
        "policy_denials": sum(1 for e in audit_events(db, project.id) if e["action"].startswith("policy.")),
        "budget": {"budget_usd": project.budget_usd, "spent_usd": project.spent_usd},
    }
