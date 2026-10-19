from sqlalchemy.orm import Session

from app.models import AgentRun, ArchitectureDecision, ChangeSet, Incident, Project, Requirement, Task


def bundle(db: Session, project: Project) -> dict:
    adrs = (
        db.query(ArchitectureDecision)
        .filter(ArchitectureDecision.project_id == project.id)
        .order_by(ArchitectureDecision.id)
        .all()
    )
    changes = db.query(ChangeSet).filter(ChangeSet.project_id == project.id).order_by(ChangeSet.id).all()
    incidents = db.query(Incident).filter(Incident.project_id == project.id).all()
    reqs = (
        db.query(Requirement)
        .filter(Requirement.project_id == project.id)
        .all()
    )
    domains: dict[str, int] = {}
    for req in reqs:
        domains[req.domain] = domains.get(req.domain, 0) + 1
    return {
        "project": {"id": project.id, "name": project.name, "mode": project.mode},
        "product": {"spec_hash": project.product_hash, "feature_domains": domains},
        "adr_decisions": [
            {"id": adr.id, "decision": adr.decision, "approved": adr.approved} for adr in adrs
        ],
        "spec_ledger": [
            {"change": ch.id, "state": ch.state, "spec_hash": ch.spec_hash} for ch in changes
        ],
        "incident_lessons": [
            {"incident": inc.id, "impact": inc.impact[:160], "rca": inc.rca[:200], "state": inc.state}
            for inc in incidents
        ],
        "policy_lessons": [
            "не выходить за write scope задачи",
            "секреты не читать; verifer опять ≠ автор",
            "production-деплой запрещён профилем (§7.4)",
        ],
    }


def prompt_block(db: Session, project: Project) -> str:
    data = bundle(db, project)
    lines = [
        f"Продукт: {data['project']['name']} (mode={data['project']['mode']}); spec={data['product']['spec_hash']}",
        "Домены требований: " + (", ".join(f"{k}:{v}" for k, v in data["product"]["feature_domains"].items()) or "-"),
    ]
    for adr in data["adr_decisions"]:
        lines.append(f"ADR {adr['id']}: {adr['decision']} (approved={adr['approved']})")
    for ch in data["spec_ledger"][-3:]:
        lines.append(f"Spec {ch['change']} [{ch['state']}] {ch['spec_hash']}")
    for inc in data["incident_lessons"][-3:]:
        lines.append(f"Инцидент {inc['incident']}: {inc['rca'] or inc['state']}")
    for lesson in data["policy_lessons"]:
        lines.append(f"Правило: {lesson}")
    return "\n".join(lines)
