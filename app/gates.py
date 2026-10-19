from sqlalchemy.orm import Session

from app.models import Approval, Project


class GateDenied(Exception):
    def __init__(self, reason: str, missing: str, required_event: str, ref: str = ""):
        self.reason = reason
        self.missing = missing
        self.required_event = required_event
        self.ref = ref
        super().__init__(reason)


PRODUCT_STATES_GREEN = {
    "PRODUCT_APPROVED": "PRODUCT_PROPOSED",
    "DESIGN_APPROVED": "PROPOSED",
    "IMPLEMENTATION_ALLOWED": "IMPLEMENTATION",
}


def product_spec_hash(project: Project) -> str:
    return f"sha256:spec-{project.id}-v{project.spec_version}"


def check_coding_gate(db: Session, project: Project) -> Approval:
    if project.state not in ("PRODUCT_APPROVED", "DESIGN_APPROVED", "IMPLEMENTATION_ALLOWED"):
        raise GateDenied(
            reason="PRODUCT_NOT_APPROVED",
            missing="product approval gate (§4.5, CORE-01)",
            required_event="PRODUCT_APPROVED",
            ref=f"project:{project.id}:state={project.state}",
        )
    approval = (
        db.query(Approval)
        .filter(
            Approval.project_id == project.id,
            Approval.purpose == "coding",
            Approval.spec_hash == product_spec_hash(project),
            Approval.status == "ACTIVE",
        )
        .one_or_none()
    )
    if approval is None:
        raise GateDenied(
            reason="MISSING_PRODUCT_APPROVAL",
            missing=f"signed approval bound to spec_hash={product_spec_hash(project)}",
            required_event="POST /v1/approvals {purpose:'coding'}",
            ref=f"project:{project.id}:approvals",
        )
    return approval


def invalidate_stale_approvals(db: Session, project: Project) -> int:
    stale = (
        db.query(Approval)
        .filter(
            Approval.project_id == project.id,
            Approval.status == "ACTIVE",
            Approval.spec_hash != product_spec_hash(project),
        )
        .all()
    )
    for approval in stale:
        approval.status = "STALE"
    return len(stale)
