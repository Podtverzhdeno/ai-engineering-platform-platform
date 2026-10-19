from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import current_user
from app.db import get_db
from app.models import ChangeSet, Evidence, Finding, new_id, Project, Release, User
from app.policy import require_member, risk_floor

router = APIRouter()

BLOCKING_SEVERITY = {"HIGH", "CRITICAL"}
SECURITY_CLASSIFICATIONS = {"AUTHORIZATION_BYPASS", "SECRET_EXFIL", "INJECTION", "IDOR", "PRIV_ESC"}


class FindingIn(BaseModel):
    classification: str
    severity: str
    file: str = ""
    line_range: list[int] = []
    scenario: str = ""
    expected: str = ""
    actual: str = ""
    reproduction: str = ""
    evidence_uri: str


class VerifyIn(BaseModel):
    verdict: str
    evidence: dict = {}
    reproduction: str = ""


class TestResultIn(BaseModel):
    suite: str
    status: str
    artifact_hash: str = "sha256:ci"
    details: str = ""


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


@router.post("/v1/changes/{change_id}/findings", status_code=201)
def create_finding(
    change_id: str,
    payload: FindingIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    change = _change(db, change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="member")
    if not payload.evidence_uri:
        raise HTTPException(status_code=422, detail={"error": "EVIDENCE_REQUIRED", "ref": "Finding Contract §11.4"})
    if payload.severity not in ("LOW", "MEDIUM", "HIGH", "CRITICAL"):
        raise HTTPException(status_code=422, detail="invalid severity")
    run_idle = change.review_cycles
    finding = Finding(
        id=new_id("F"),
        change_id=change.id,
        classification=payload.classification,
        severity=payload.severity,
        source_agent=user.name,
        file=payload.file,
        line_range=payload.line_range,
        scenario=payload.scenario,
        expected=payload.expected,
        actual=payload.actual,
        reproduction=payload.reproduction,
        evidence_uri=payload.evidence_uri,
    )
    db.add(finding)
    db.flush()
    audit.append(db, project.id, user.id, "finding.created", {"finding": finding.id, "severity": payload.severity})
    db.commit()
    return {"id": finding.id, "verifier_state": finding.verifier_state, "review_cycles": find_cycles(change)}


def find_cycles(change: ChangeSet) -> int:
    return change.review_cycles


@router.post("/v1/findings/{finding_id}/verify")
def verify_finding(
    finding_id: str,
    payload: VerifyIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="finding not found")
    change = _change(db, finding.change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="member")
    if payload.verdict not in ("CONFIRMED", "REJECTED", "INCONCLUSIVE"):
        raise HTTPException(status_code=422, detail="invalid verdict")
    if payload.verdict != "REJECTED" and not (payload.reproduction or payload.evidence):
        raise HTTPException(status_code=422, detail={"error": "VERIFIER_EVIDENCE_REQUIRED", "ref": "ACC-08"})
    finding.verifier_state = payload.verdict
    finding.verifier_evidence = payload.evidence or {"reproduction": payload.reproduction}
    if payload.verdict == "CONFIRMED":
        finding.resolution = "OPEN"
    elif payload.verdict == "REJECTED":
        finding.resolution = "CLOSED_FALSE_POSITIVE"
    db.flush()
    audit.append(db, project.id, user.id, "finding.verified", {"finding": finding.id, "verdict": payload.verdict})
    db.commit()
    return {"id": finding.id, "verifier_state": finding.verifier_state, "resolution": finding.resolution}


@router.post("/v1/changes/{change_id}/review-cycle")
def bump_review_cycle(
    change_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    change = _change(db, change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="member")
    change.review_cycles += 1
    db.flush()
    if change.review_cycles >= 3:
        audit.append(db, project.id, user.id, "review.convergence_blocked", {"change": change.id})
        db.commit()
        raise HTTPException(
            status_code=409,
            detail={"error": "CONVERGENCE_BLOCKED", "cycles": change.review_cycles, "action": "root-cause analysis (§11.5, R-11)"},
        )
    db.commit()
    return {"review_cycles": change.review_cycles}


def blocking_findings(db: Session, change: ChangeSet) -> list[Finding]:
    return [
        f
        for f in db.query(Finding).filter(Finding.change_id == change.id).all()
        if f.verifier_state == "CONFIRMED"
        and f.resolution == "OPEN"
        and f.severity in BLOCKING_SEVERITY
        and f.classification in SECURITY_CLASSIFICATIONS
    ]


@router.post("/v1/changes/{change_id}/test-results", status_code=201)
def record_test(
    change_id: str,
    payload: TestResultIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    change = _change(db, change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="member")
    if payload.status not in ("PASS", "FAIL"):
        raise HTTPException(status_code=422, detail="invalid status")
    evidence = Evidence(
        id=new_id("EV"),
        run_id=change.id,
        artifact_hash=payload.artifact_hash,
        links=[{"suite": payload.suite, "status": payload.status, "details": payload.details}],
    )
    db.add(evidence)
    db.flush()
    audit.append(db, project.id, user.id, "test.recorded", {"change": change.id, "suite": payload.suite, "status": payload.status})
    db.commit()
    return {"evidence_id": evidence.id}


def release_readiness(db: Session, change: ChangeSet) -> dict:
    project = db.get(Project, change.project_id)
    blocking = blocking_findings(db, change)
    tests = db.query(Evidence).filter(Evidence.run_id == change.id).all()
    suites = {}
    for e in tests:
        for link in e.links:
            suites[link["suite"]] = link["status"]
    gaps = []
    risk = risk_floor(list(change.affected_paths or []), list(change.scope or []))
    if risk in ("HIGH", "CRITICAL") and not change.artifacts.get("threat_model_md"):
        gaps.append("threat_model_missing")
    if suites.get("unit") != "PASS":
        gaps.append("unit_tests_missing")
    if any(p.startswith(("migrations/", "db/")) for p in (change.affected_paths or [])) and suites.get("contract") != "PASS":
        gaps.append("contract_tests_required_for_migrations")
    if "prod_migration" in (change.scope or []) and "restore plan" not in (change.artifacts.get("operations_md") or ""):
        gaps.append("migration_restore_plan_missing")
    if blocking:
        gaps.append(f"blocking_security_findings:{[f.id for f in blocking]}")
    design_ok = change.state in ("DESIGN_APPROVED", "REVIEW_READY", "MERGED")
    if not design_ok:
        gaps.append(f"design_state:{change.state}")
    verdict = "NO_GO" if gaps else "GO"
    return {"verdict": verdict, "risk": risk, "gaps": gaps}


@router.get("/v1/changes/{change_id}/readiness")
def readiness(
    change_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    change = _change(db, change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="viewer")
    return release_readiness(db, change)
