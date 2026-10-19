import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db import get_db
from app.models import ChangeSet, EvalReport, Finding, Project, User, new_id
from app.policy import require_member

router = APIRouter()


class ReportIn(BaseModel):
    project_id: str
    planted_cases: dict[str, str]
    reviewer_results: dict[str, str]


class LiveIn(BaseModel):
    project_id: str
    change_ids: list[str] = []


def _project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@router.post("/v1/evals/reports", status_code=201)
def create_report(
    payload: ReportIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    if not payload.planted_cases:
        raise HTTPException(status_code=422, detail="planted_cases required")
    project = _project(db, payload.project_id)
    require_member(db, project, user.id, min_role="member")
    total = len(payload.planted_cases)
    recalled = sum(1 for case, expected in payload.planted_cases.items() if payload.reviewer_results.get(case) == expected)
    confirmed = [c for c, verdict in payload.reviewer_results.items() if verdict == "CONFIRMED"]
    true_positives = [c for c in confirmed if c in payload.planted_cases]
    precision = round(len(true_positives) / len(confirmed), 3) if confirmed else 0.0
    recall = round(recalled / total, 3)
    ds_seed = json.dumps(sorted(payload.planted_cases.items()), sort_keys=True)
    dataset_version = "v1-" + hashlib.sha256(ds_seed.encode()).hexdigest()[:12]
    report = EvalReport(
        id=new_id("EVL"),
        project_id=project.id,
        dataset_version=dataset_version,
        results={"reviewer": payload.reviewer_results, "planted": payload.planted_cases},
        metrics={"recall_planted": recall, "reviewer_precision": precision},
        model="review-advisor-v0",
    )
    db.add(report)
    db.flush()
    db.commit()
    return {"id": report.id, "metrics": report.metrics, "dataset_version": dataset_version}


@router.get("/v1/evals/reports/{report_id}")
def get_report(
    report_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    report = db.get(EvalReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    project = _project(db, report.project_id)
    require_member(db, project, user.id, min_role="viewer")
    return {
        "id": report.id,
        "dataset_version": report.dataset_version,
        "metrics": report.metrics,
        "model": report.model,
        "results": report.results,
    }


@router.post("/v1/evals/live", status_code=201)
def create_live_report(
    payload: LiveIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Eval Lab по живым аудитам: planted = finding.classification == "planted".

    recall_planted — доля planted-находок, подтверждённых верификатором.
    reviewer_precision — точность (CONFIRMED)/(CONFIRMED+REJECTED) по не-planted находкам
    (planted точность считается отдельно: CONFIRMED∩PLANTED / CONFIRMED).
    """
    project = _project(db, payload.project_id)
    require_member(db, project, user.id, min_role="member")
    change_ids = (
        [row.id for row in db.query(ChangeSet.id).filter(ChangeSet.project_id == project.id).all()]
    )
    findings = db.query(Finding).filter(Finding.change_id.in_(change_ids) if change_ids else False).all()
    if payload.change_ids:
        allow = set(payload.change_ids)
        findings = [f for f in findings if f.change_id in allow]
    verdicts = {f.id: (f.verifier_state or "OPEN", (f.classification or "").strip().lower() == "planted") for f in findings}
    planted = {fid for fid, (verdict, is_planted) in verdicts.items() if is_planted}
    if not verdicts:
        raise HTTPException(status_code=422, detail="no findings audited yet")
    confirmed_planted = sum(1 for fid in planted if verdicts[fid][0] == "CONFIRMED")
    recall = round(confirmed_planted / len(planted), 3) if planted else 0.0
    reviewed = [verdict for verdict, is_planted in verdicts.values() if not is_planted and verdict in ("CONFIRMED", "REJECTED")]
    precision = round(sum(1 for verdict in reviewed if verdict == "CONFIRMED") / len(reviewed), 3) if reviewed else 0.0
    ds_seed = json.dumps(sorted(json.dumps([fid, verdicts[fid][0], verdicts[fid][1]]) for fid in verdicts), sort_keys=True)
    dataset_version = "live-" + hashlib.sha256(ds_seed.encode()).hexdigest()[:12]
    report = EvalReport(
        id=new_id("EVL"),
        project_id=project.id,
        dataset_version=dataset_version,
        results={"verdicts": {fid: verdicts[fid][0] for fid in sorted(verdicts)}, "planted": sorted(planted)},
        metrics={
            "recall_planted": recall,
            "reviewer_precision": precision,
            "planted_total": len(planted),
            "findings_total": len(verdicts),
        },
        model="review-advisor-v0",
    )
    db.add(report)
    db.flush()
    db.commit()
    return {"id": report.id, "metrics": report.metrics, "dataset_version": dataset_version}
