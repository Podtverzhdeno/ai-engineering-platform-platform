from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db import get_db
from app.models import Project, User
from app.policy import require_member
from app.services import memory

router = APIRouter()


@router.get("/v1/memory/{project_id}")
def read_memory(
    project_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    require_member(db, project, user.id, min_role="viewer")
    return memory.bundle(db, project)


@router.get("/v1/memory/{project_id}/prompt")
def read_memory_prompt(
    project_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    require_member(db, project, user.id, min_role="viewer")
    return {"prompt": memory.prompt_block(db, project)}
