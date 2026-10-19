from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import audit
from app.models import ArchitectureDecision, ChangeSet, Project

LAYERS = {
    "frontend": ["React/SPA", "Vue/SPA", "SSR (Next.js)", "SSR (SvelteKit)", "Хост-логика без SPA"],
    "backend": ["Python/FastAPI", "TypeScript/NestJS", "Java/Spring", "Go/chi"],
    "db": ["PostgreSQL", "SQLite", "DocumentDB (Mongo)", "Managed KV"],
    "broker": ["none (sync only)", "Postgres LISTEN/NOTIFY", "Kafka", "RabbitMQ"],
}

CRITERIA = ["security", "maintainability", "ecosystem", "performance", "tco", "hosting", "observability", "lock_in"]
WEIGHTS = {c: 1.0 for c in CRITERIA}
PENALTY = {"lock_in": 1.5, "tco": 1.2, "security": 1.3}


def score_option(option: str, layer: str, constraints: list[str], nfr: dict) -> float:
    score = 5.0
    checks = _compatibility(layer, option, constraints, nfr)
    for bad in checks:
        score -= PENALTY.get(bad, 1.0) * 2.5
    if "self-hosted" in constraints and any(x in option for x in ("Managed KV", "DocumentDB")):
        score -= 2.0
    if nfr.get("ml_semantics") and layer == "backend" and "Python" in option:
        score += 1.5
    if nfr.get("seo") and layer == "frontend" and "SPA" in option:
        score -= 1.0
    if nfr.get("transactions") and layer == "db" and any(x in option for x in ("KV", "DocumentDB")):
        score -= 1.5
    return round(max(score, 0.0), 2)


def _compatibility(layer: str, option: str, constraints: list[str], nfr: dict) -> list[str]:
    issues = []
    if layer == "backend" and "Enterprise" in constraints and "Java" not in option:
        issues.append("maintainability")
    if layer == "db" and "transactions" in constraints and any(x in option for x in ("KV", "DocumentDB")):
        issues.append("performance")
    if "no_ops_complexity" in constraints and option == "RabbitMQ":
        issues.append("tco")
    if nfr.get("regional_data") and "Managed" in option:
        issues.append("lock_in")
    return issues


def recommend(db: Session, project: Project, change: ChangeSet, layer: str, constraints: list[str], nfr: dict) -> dict:
    if layer not in LAYERS:
        raise HTTPException(status_code=422, detail="unknown layer")
    matrix = [
        {
            "option": option,
            "score": score_option(option, layer, constraints, nfr),
            "known_tradeoffs": _compatibility(layer, option, constraints, nfr),
        }
        for option in LAYERS[layer]
    ]
    matrix.sort(key=lambda row: -row["score"])
    candidates = [row for row in matrix if row["known_tradeoffs"] == []][:3] or matrix[:3]
    adr = ArchitectureDecision(
        id=f"ADR-{layer[:4].upper()}-{change.id[-4:]}",
        project_id=project.id,
        decision=f"candidate-selection-{layer}",
        alternatives=[row["option"] for row in candidates],
        tradeoffs=[row["known_tradeoffs"] for row in candidates],
        approved=False,
    )
    db.add(adr)
    db.flush()
    audit.append(
        db,
        project.id,
        "system",
        "tech_advisor.recommendation",
        {"change_id": change.id, "layer": layer, "recommended": candidates[0]["option"]},
    )
    return {
        "layer": layer,
        "matrix": matrix,
        "recommended": candidates[0]["option"],
        "alternatives": [row["option"] for row in candidates[1:]],
        "reason": "ranked by constraint matrix; PoC required for non-obvious pick (ADR-TECH)",
        "adr_id": adr.id,
        "verification_required": ["PoC benchmark", "ops review"] if nfr.get("transactions") else ["ops review"],
        "status": "PROPOSED",
    }


def confirm(db: Session, project: Project, adr_id: str, chosen: str) -> dict:
    adr = db.get(ArchitectureDecision, adr_id)
    if adr is None:
        raise HTTPException(status_code=404, detail="adr not found")
    adr.decision = f"{adr.decision}={chosen}"
    adr.approved = True
    db.flush()
    audit.append(db, project.id, "system", "adr.approved", {"adr_id": adr.id, "chosen": chosen})
    return {"adr_id": adr.id, "decision": adr.decision}
