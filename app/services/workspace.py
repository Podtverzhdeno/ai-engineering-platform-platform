import json
import os
import shutil

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Project, Task, ChangeSet

WORKSPACES_ENV = "AIPLATFORM_WORKSPACES"


def root() -> str:
    default = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".workspaces")
    return os.environ.get(WORKSPACES_ENV, default)


def build_task_contract(project: Project, change: ChangeSet, task: Task, spec_hash: str) -> dict:
    return {
        "task_id": task.id,
        "change_id": change.id,
        "spec_hash": spec_hash,
        "write_scope": task.write_scope,
        "read_scope": task.read_scope,
        "acceptance_ids": task.acceptance_ids,
        "permissions_profile": task.agent,
        "limits": task.limits,
        "no_secrets": True,
        "sandbox": "isolated-runner-container",
    }


def materialize(db: Session, project: Project, change: ChangeSet, task: Task, spec_hash: str) -> str:
    base = root()
    path = os.path.join(base, task.id)
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)
    contract = build_task_contract(project, change, task, spec_hash)
    with open(os.path.join(path, "task-contract.json"), "w", encoding="utf-8") as handle:
        json.dump(contract, handle, ensure_ascii=False, indent=2)
    return path


def cleanup(session_path: str) -> None:
    if session_path and os.path.isdir(session_path):
        shutil.rmtree(session_path, ignore_errors=True)
