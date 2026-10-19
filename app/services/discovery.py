import hashlib
import json

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import audit
from app.models import InterviewSession, new_id, Project, Requirement
from app.services import state_machine

QUESTION_BANK = [
    {"id": "Q-PROD-001", "area": "problem", "priority": 5, "text": "Что сейчас не удаётся сделать? Как это решается сегодня?"},
    {"id": "Q-PROD-002", "area": "users", "priority": 5, "text": "Кто основные и вторичные пользователи? Кто запрещён?"},
    {"id": "Q-PROD-003", "area": "value", "priority": 5, "text": "Какой измеримый результат должен улучшиться?"},
    {"id": "Q-PROD-004", "area": "scenarios", "priority": 5, "text": "Как выглядит полный happy path? Где пользователь начинает и заканчивает?"},
    {"id": "Q-PROD-005", "area": "statuses", "priority": 4, "text": "Какие состояния возможны и кто переводит объект между ними?"},
    {"id": "Q-PROD-006", "area": "exceptions", "priority": 4, "text": "Что происходит при пустых полях, дублях, тайм-аутах, отсутствии сети, нехватке прав?"},
    {"id": "Q-PROD-007", "area": "priorities", "priority": 5, "text": "Без каких функций продукт не выполняет обещание? Что откладывается сознательно?"},
    {"id": "Q-PROD-008", "area": "data", "priority": 4, "text": "Что является источником истины? Какие данные можно удалять и экспортировать?"},
    {"id": "Q-PROD-009", "area": "notifications", "priority": 2, "text": "Какие события требуют уведомлений и как избегать повторной доставки?"},
    {"id": "Q-PROD-010", "area": "unforeseen", "priority": 4, "text": "Какие три сценария пользователь точно не должен уметь реализовать?"},
    {"id": "Q-ARCH-001", "area": "frontend", "priority": 5, "text": "Есть ли предпочтение frontend-фреймворку, SSR/SPA? Требования к SEO и скорости?"},
    {"id": "Q-ARCH-002", "area": "backend", "priority": 5, "text": "Нужен ли API? Известны ли язык/фреймворк?"},
    {"id": "Q-ARCH-003", "area": "data", "priority": 5, "text": "Какие объёмы данных, транзакции, retention?"},
    {"id": "Q-ARCH-004", "area": "authz", "priority": 5, "text": "Кто выполняет вход, какие роли и permissions, нужна ли multi-tenancy?"},
    {"id": "Q-ARCH-005", "area": "secrets", "priority": 4, "text": "Есть ли внешние ключи/сертификаты? Кто их хранит и ротирует?"},
    {"id": "Q-ARCH-006", "area": "audit", "priority": 4, "text": "Какие действия важно фиксировать в журнале и сколько хранить?"},
    {"id": "Q-ARCH-007", "area": "scale", "priority": 3, "text": "Ожидаемая нагрузка, p95 latency, размеры сообщений?"},
    {"id": "Q-ARCH-008", "area": "release", "priority": 4, "text": "Нужны ли staging, feature flags, canary, миграция без остановки?"},
    {"id": "Q-ARCH-009", "area": "legal", "priority": 3, "text": "Политики доступа, ПДн, лицензии?"},
    {"id": "Q-ARCH-010", "area": "architecture", "priority": 4, "text": "Какие технические компромиссы неприемлемы: сложность DevOps, lock-in, стоимость?"},
]

CRITICAL_AREAS = {"problem", "users", "value", "scenarios", "priorities", "frontend", "backend", "data", "authz"}


def start_intake(db: Session, project: Project) -> InterviewSession:
    if project.state != "CREATED":
        raise HTTPException(status_code=409, detail={"error": "INVALID_STATE", "state": project.state})
    session = InterviewSession(id=new_id("ITW"), project_id=project.id, revision=1)
    db.add(session)
    db.flush()
    state_machine.transition(db, project, "START_INTERVIEW")
    audit.append(db, project.id, "system", "intake.started", {"interview_id": session.id})
    return session


def _unanswered(session: InterviewSession) -> list[dict]:
    answered = {a.get("question_id") for a in session.answers}
    return [q for q in QUESTION_BANK if q["id"] not in answered]


def next_round(session: InterviewSession) -> list[dict]:
    remaining = _unanswered(session)
    remaining.sort(key=lambda q: (-q["priority"], q["id"]))
    return remaining[:7] if remaining else []


def gaps(session: InterviewSession) -> list[dict]:
    gaps_out = []
    for q in QUESTION_BANK:
        if q["area"] in CRITICAL_AREAS and not any(
            a.get("question_id") == q["id"] and a.get("status") in ("CONFIRMED", "N_A") for a in session.answers
        ):
            gaps_out.append({"question_id": q["id"], "area": q["area"], "text": q["text"]})
    return gaps_out


def submit_answers(db: Session, session: InterviewSession, project: Project, answers: list[dict]) -> int:
    bank_ids = {q["id"] for q in QUESTION_BANK}
    count = 0
    for item in answers:
        key = f'{project.id}:Q:{item["question_id"]}'
        previous = [
            a for a in session.answers
            if a.get("key") == key and a.get("status") not in ("N_A",)
        ]
        conflicting = any(
            prev.get("answer") != item.get("answer") and prev.get("status") == "CONFIRMED" for prev in previous
        )
        new_answer = {
            "question_id": item["question_id"],
            "answer": item.get("answer"),
            "source": item.get("source", "USER_EXPLICIT"),
            "status": "CONFIRMED" if item.get("source", "USER_EXPLICIT") == "USER_EXPLICIT" else "OPEN",
            "key": key,
            "depends": [],
            "provenance": item.get("source", "USER_EXPLICIT"),
        }
        if conflicting:
            new_answer["status"] = "BLOCKER"
            new_answer["open_questions"] = [f"Противоречие с предыдущим ответом на {item['question_id']}"]
        session.answers = [*session.answers, user_canonical_revision(new_answer)]
        if item["question_id"] in bank_ids:
            count += 1
    db.flush()
    audit.append(db, project.id, "system", "intake.answers", {"count": len(answers)})
    return count


def _hash_answer(answer: dict) -> str:
    return hashlib.sha256(json.dumps(answer, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def user_canonical_revision(answer: dict) -> dict:
    return {**answer, "revision": answer.get("revision", 1), "hash": _hash_answer(answer)}


def suggest_feature(db: Session, session: InterviewSession, project: Project, feature_text: str) -> dict:
    entry = {
        "feature_text": feature_text,
        "source": "MODEL_SUGGESTED",
        "status": "OPEN",
        "order": len(session.questions),
    }
    session.questions = [*session.questions, entry]
    db.flush()
    audit.append(db, project.id, "system", "intake.model_suggestion", {"source": "MODEL_SUGGESTED"})
    return entry


def get_ledger(session: InterviewSession) -> dict:
    confirmed = [a for a in session.answers if a.get("status") == "CONFIRMED"]
    blockers = [a for a in session.answers if a.get("status") == "BLOCKER"]
    suggestions = [q for q in session.questions if q.get("source") == "MODEL_SUGGESTED"]
    return {
        "confirmed": confirmed,
        "blockers": blockers,
        "suggestions": suggestions,
        "open": [a for a in session.answers if a.get("status") == "OPEN"],
    }


def finalize(db: Session, session: InterviewSession, project: Project) -> dict:
    if session.status != "IN_PROGRESS":
        raise HTTPException(status_code=409, detail={"error": "INTERVIEW_NOT_IN_PROGRESS"})
    open_gaps = gaps(session)
    if open_gaps:
        raise HTTPException(
            status_code=409,
            detail={"error": "CRITICAL_GAPS_OPEN", "gaps": open_gaps},
        )
    blocker_answers = [a for a in session.answers if a.get("status") == "BLOCKER"]
    if blocker_answers:
        raise HTTPException(status_code=409, detail={"error": "CONFLICTS_RESOLVE_REQUIRED", "blockers": blocker_answers})

    features = []
    domain_map = {
        "problem": "product", "users": "identity", "value": "metrics", "scenarios": "ux",
        "statuses": "domain", "exceptions": "resilience", "priorities": "scope", "data": "data",
        "frontend": "frontend", "backend": "api", "authz": "auth", "secrets": "security",
        "audit": "audit", "architecture": "architecture",
    }
    for answer in session.answers:
        q = next((qq for qq in QUESTION_BANK if qq["id"] == answer.get("question_id")), None)
        if q and answer.get("status") == "CONFIRMED" and answer.get("source") == "USER_EXPLICIT":
            domain = domain_map.get(q["area"], "general")
            feature_text = q["text"]
            existing = next((r for r in features if r["domain"] == domain), None)
            if existing:
                existing["scenarios"].append(answer.get("answer", ""))
            else:
                features.append({"domain": domain, "text": feature_text, "scenarios": [answer.get("answer", "")]})

    for feature in features:
        # id уникален в пределах проекта: идентичные ответы в разных проектах не конфликтуют
        seed = project.id + "|" + json.dumps(feature, sort_keys=True, ensure_ascii=False)
        req = Requirement(
            id=f"REQ-{hashlib.sha256(seed.encode()).hexdigest()[:8]}",
            project_id=project.id,
            domain=feature["domain"],
            text=feature["text"],
            scenarios=feature["scenarios"],
            status="CONFIRMED",
            source="USER_EXPLICIT",
        )
        db.add(req)

    pack = {
        "project_id": project.id,
        "mode": project.mode,
        "interview_revision": session.revision,
        "product_summary": "Consolidated from confirmed answers",
        "approved_features": [feature["text"] for feature in features],
        "proposed_features": [s["feature_text"] for s in session.questions if s.get("source") == "MODEL_SUGGESTED"],
        "critical_open_questions": [],
        "constraints": [a["answer"] for a in session.answers if a.get("question_id") == "Q-ARCH-010" and a.get("answer")],
        "acceptance_coverage": {feature["text"]: [f"SC-{i + 1}" for i, s in enumerate(feature["scenarios"])] for feature in features},
        "approval_status": "PENDING_USER",
        "immutable_spec_hash": product_hash_of(project, session, features),
    }

    project.product_hash = pack["immutable_spec_hash"]
    state_machine.transition(db, project, "PRODUCT_PROPOSED")
    audit.append(db, project.id, "system", "product.proposed", {"spec_hash": pack["immutable_spec_hash"], "features": len(features)})
    session.status = "FINALIZED"
    db.flush()
    return pack


def product_hash_of(project: Project, session: InterviewSession, features: list) -> str:
    canonical = {
        "project": project.id,
        "revision": session.revision,
        "features": sorted(json.dumps(features, sort_keys=True, ensure_ascii=False)),
    }
    return "sha256:" + hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
