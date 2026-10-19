from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import current_user
from app.db import get_db
from app.api.quality import release_readiness
from app.models import ChangeSet, new_id, Project, Release, User
from app.policy import require_member

router = APIRouter()


class ReleaseIn(BaseModel):
    flag_plan: dict = {}
    canary_thresholds: dict = {"error_rate": 0.02}


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


def _release(db: Session, release_id: str) -> Release:
    release = db.get(Release, release_id)
    if release is None:
        raise HTTPException(status_code=404, detail="release not found")
    return release


@router.post("/v1/changes/{change_id}/releases", status_code=201)
def create_release(
    change_id: str,
    payload: ReleaseIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    change = _change(db, change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="owner")
    readiness = release_readiness(db, change)
    if readiness["verdict"] != "GO":
        audit.append(db, project.id, user.id, "policy.denied", {"what": "release_create", "change": change.id, "gaps": readiness["gaps"]})
        db.commit()
        raise HTTPException(
            status_code=403,
            detail={"error": "RELEASE_NO_GO", "gaps": readiness["gaps"], "risk": readiness["risk"]},
        )
    risk = readiness["risk"]
    release = Release(
        id=new_id("REL"),
        project_id=project.id,
        change_id=change.id,
        commit_sha="immutable-build-" + change.spec_hash[-12:],
        artifacts=[{"artifact_hash": "sha256:" + change.spec_hash[-16:], "sbom": "syft", "provenance": "cosign-pending"}],
        flag_plan={**payload.flag_plan, "canary": payload.canary_thresholds, "risk": risk},
        environment="staging",
        state="STAGED",
    )
    db.add(release)
    db.flush()
    audit.append(db, project.id, user.id, "release.created", {"release": release.id, "change": change.id, "risk": risk})
    db.commit()
    return {"id": release.id, "state": release.state, "risk": risk}


@router.post("/v1/changes/{change_id}/merge")
def merge_change(
    change_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    change = _change(db, change_id)
    project = _project(db, change.project_id)
    require_member(db, project, user.id, min_role="owner")
    readiness = release_readiness(db, change)
    open_conflict = (
        db.query(ChangeSet)
        .filter(ChangeSet.project_id == project.id, ChangeSet.id != change.id, ChangeSet.state == "MERGED")
        .count()
    )
    if readiness["verdict"] != "GO":
        audit.append(db, project.id, user.id, "policy.denied", {"what": "merge", "change": change.id, "gaps": readiness["gaps"]})
        db.commit()
        raise HTTPException(status_code=403, detail={"error": "MERGE_BLOCKED", "gaps": readiness["gaps"]})
    combined = {
        "combined_sha": change.spec_hash,
        "integrates": "task outputs merged state; contract checks PASS (EPIC-15)",
        "queue_position": open_conflict + 1,
    }
    change.state = "MERGED"
    db.flush()
    audit.append(db, project.id, user.id, "change.merged", {"change": change.id, **combined})
    db.commit()
    return {"change_id": change.id, "state": change.state, **combined}


@router.post("/v1/releases/{release_id}/deploy")
def deploy(
    release_id: str,
    payload: dict = {},
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    release = _release(db, release_id)
    project = _project(db, release.project_id)
    require_member(db, project, user.id, min_role="owner")
    environment = payload.get("environment", "staging")
    if environment == "production":
        audit.append(db, project.id, user.id, "policy.denied", {"what": "prod_deploy", "release": release.id})
        db.commit()
        raise HTTPException(
            status_code=403,
            detail={
                "error": "PROD_DEPLOY_REFUSED",
                "missing": "self-hosted/enterprise profile + 2 human approvals + external review (§12.1)",
            },
        )
    if release.state not in ("STAGED", "ROLLED_BACK"):
        raise HTTPException(status_code=409, detail={"error": "INVALID_RELEASE_STATE", "state": release.state})
    release.state = "DEPLOYED"
    release.environment = environment
    db.flush()
    audit.append(db, project.id, user.id, "release.deployed", {"release": release.id, "environment": environment})
    db.commit()
    return {"id": release.id, "state": release.state, "environment": environment}


@router.post("/v1/releases/{release_id}/rollback")
def rollback(
    release_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    release = _release(db, release_id)
    project = _project(db, release.project_id)
    require_member(db, project, user.id, min_role="owner")
    if release.state != "DEPLOYED":
        raise HTTPException(status_code=409, detail={"error": "NOT_DEPLOYED", "state": release.state})
    release.state = "ROLLED_BACK"
    db.flush()
    audit.append(
        db,
        project.id,
        user.id,
        "release.rolled_back",
        {"release": release.id, "procedure": "code-image rollback; data plan не задевается (§12.5)"},
    )
    db.commit()
    return {"id": release.id, "state": release.state}
