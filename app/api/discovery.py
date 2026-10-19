from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db import get_db
from app.models import InterviewSession, Project, User
from app.policy import require_member
from app.services import discovery

router = APIRouter()


class AnswersIn(BaseModel):
    answers: list[dict]


class SuggestIn(BaseModel):
    feature_text: str


def _get_session(db: Session, session_id: str) -> InterviewSession:
    session = db.get(InterviewSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="interview not found")
    return session


def _get_project(db: Session, project_id: str) -> Project:
    return db.get(Project, project_id)


@router.get("/v1/projects/{project_id}/interviews")
def list_interviews(
    project_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    project = _get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    require_member(db, project, user.id, min_role="viewer")
    sessions = (
        db.query(InterviewSession)
        .filter(InterviewSession.project_id == project_id)
        .order_by(InterviewSession.id)
        .all()
    )
    return [{"id": s.id, "status": s.status, "revision": s.revision} for s in sessions]


@router.post("/v1/interviews", status_code=201)
def start_intake(
    payload: dict,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = _get_project(db, payload["project_id"])
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    require_member(db, project, user.id, min_role="member")
    session = discovery.start_intake(db, project)
    db.commit()
    return {"interview_id": session.id, "round": discovery.next_round(session), "state": project.state}


@router.get("/v1/interviews/{session_id}")
def get_interview(
    session_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    session = _get_session(db, session_id)
    project = _get_project(db, session.project_id)
    require_member(db, project, user.id, min_role="viewer")
    return {
        "id": session.id,
        "status": session.status,
        "round": discovery.next_round(session),
        "answers": session.answers,
        "gaps": discovery.gaps(session),
        "ledger": discovery.get_ledger(session),
    }


@router.post("/v1/interviews/{session_id}/answers")
def submit_answers(
    session_id: str,
    payload: AnswersIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    session = _get_session(db, session_id)
    project = _get_project(db, session.project_id)
    require_member(db, project, user.id, min_role="member")
    count = discovery.submit_answers(db, session, project, payload.answers)
    db.commit()
    return {"recorded": count, "round": discovery.next_round(session), "gaps": discovery.gaps(session)}


@router.post("/v1/interviews/{session_id}/suggest", status_code=201)
def suggest_feature(
    session_id: str,
    payload: SuggestIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    session = _get_session(db, session_id)
    project = _get_project(db, session.project_id)
    require_member(db, project, user.id, min_role="member")
    entry = discovery.suggest_feature(db, session, project, payload.feature_text)
    db.commit()
    return {**entry, "note": "MODEL_SUGGESTED нельзя подтвердить автоматически — решает пользователь"}


@router.post("/v1/interviews/{session_id}/finalize")
def finalize(
    session_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    session = _get_session(db, session_id)
    project = _get_project(db, session.project_id)
    require_member(db, project, user.id, min_role="member")
    pack = discovery.finalize(db, session, project)
    db.commit()
    return {"approval_pack": pack, "state": project.state}


@router.get("/v1/interviews/{session_id}/gaps")
def get_gaps(
    session_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    session = _get_session(db, session_id)
    project = _get_project(db, session.project_id)
    require_member(db, project, user.id, min_role="viewer")
    return {"gaps": discovery.gaps(session), "ledger": discovery.get_ledger(session)}
