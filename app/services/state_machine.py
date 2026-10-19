from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import audit
from app.models import Project

TRANSITIONS = {
    ("CREATED", "START_INTERVIEW"): "INTERVIEW_IN_PROGRESS",
    ("INTERVIEW_IN_PROGRESS", "PRODUCT_PROPOSED"): "PRODUCT_PROPOSED",
    ("PRODUCT_PROPOSED", "PRODUCT_APPROVED"): "PRODUCT_APPROVED",
    ("PRODUCT_APPROVED", "DESIGN_PROPOSED"): "DESIGNING",
    ("DESIGNING", "DESIGN_APPROVED"): "DESIGN_APPROVED",
    ("DESIGN_APPROVED", "PLAN_READY"): "IMPLEMENTATION_ALLOWED",
    ("IMPLEMENTATION_ALLOWED", "ALL_TASKS_DONE"): "REVIEW_READY",
    ("REVIEW_READY", "QUALITY_PASSED"): "RELEASE_CANDIDATE",
    ("RELEASE_CANDIDATE", "HUMAN_RELEASE_APPROVAL"): "DEPLOYABLE",
    ("DEPLOYABLE", "DEPLOYED"): "DEPLOYED",
    ("DEPLOYED", "ROLLBACK"): "RECOVERY",
    ("RECOVERY", "RECOVERED"): "DEPLOYED",
}


def transition(db: Session, project: Project, event: str) -> str:
    key = (project.state, event)
    if key not in TRANSITIONS:
        raise HTTPException(
            status_code=409,
            detail={"error": "INVALID_TRANSITION", "state": project.state, "event": event},
        )
    project.state = TRANSITIONS[key]
    db.flush()
    audit.append(db, project.id, "system", "state.transition", {"event": event, "to": project.state})
    return project.state
