from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Membership, ROLE_RANK, Project

ALLOWED_CLASSIFICATION = {"PUBLIC", "PRIVATE", "RESTRICTED"}
ALLOWED_MODE = {"NEW", "CHANGE", "BUGFIX", "REFACTOR", "MIGRATION", "AUDIT", "INCIDENT"}


class PermissionDenied(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status_code=403, detail=detail)


def get_membership(db: Session, project_id: str, user_id: str) -> Membership | None:
    return (
        db.query(Membership)
        .filter(Membership.project_id == project_id, Membership.user_id == user_id)
        .one_or_none()
    )


def require_member(db: Session, project: Project, user_id: str, min_role: str = "member") -> Membership:
    membership = get_membership(db, project.id, user_id)
    if membership is None or ROLE_RANK[membership.role] < ROLE_RANK[min_role]:
        raise PermissionDenied("member role required")
    return membership


PROTECTED_PREFIXES = ("app/policy.py", "app/audit.py", "app/gates.py", "app/models.py", "tests/", ".github/")


def risk_floor(affected_paths: list[str], change_types: list[str]) -> str:
    floor = "LOW"
    for path in affected_paths:
        if path.startswith(PROTECTED_PREFIXES):
            floor = "CRITICAL"
        elif path.startswith(("migrations/", "db/")) or any(
            t in change_types for t in ("prod_migration", "data_deletion")
        ):
            floor = max(floor, "HIGH", key=_rank)
        elif any(t in change_types for t in ("auth_permissions", "payment", "secrets", "ci_policy")):
            floor = max(floor, "HIGH", key=_rank)
        elif any(t in change_types for t in ("api_surface", "business_rule", "feature")):
            floor = max(floor, "MEDIUM", key=_rank)
    for ctype in change_types:
        if ctype == "disallowed_llm_provider":
            return "DENY"
    return floor


def _rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else -1


LEVELS = ["LOW", "MEDIUM", "HIGH", "CRITICAL", "DENY"]
