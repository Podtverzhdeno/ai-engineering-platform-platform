import datetime as dt
import hashlib
import json

from sqlalchemy.orm import Session

from app.models import AuditEvent

GENESIS = "0" * 64


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _row_hash(prev_hash: str, project_id: str, actor_id: str, action: str, payload: dict, ts: str) -> str:
    material = "|".join([prev_hash, project_id, actor_id, action, _canonical(payload), ts])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _event_ts(event: AuditEvent) -> str:
    return event.ts_text


def append(db: Session, project_id: str, actor_id: str, action: str, payload: dict | None = None) -> AuditEvent:
    last: AuditEvent | None = (
        db.query(AuditEvent).filter(AuditEvent.project_id == project_id).order_by(AuditEvent.id.desc()).first()
    )
    prev_hash = last.row_hash if last else GENESIS
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    event = AuditEvent(
        project_id=project_id,
        actor_id=actor_id,
        action=action,
        payload=payload or {},
        prev_hash=prev_hash,
        row_hash=_row_hash(prev_hash, project_id, actor_id, action, payload or {}, now),
        ts=dt.datetime.fromisoformat(now),
        ts_text=now,
    )
    db.add(event)
    db.flush()
    return event


def verify(db: Session, project_id: str) -> tuple[bool, int | None]:
    events = db.query(AuditEvent).filter(AuditEvent.project_id == project_id).order_by(AuditEvent.id).all()
    prev = GENESIS
    for event in events:
        expected = _row_hash(prev, event.project_id, event.actor_id, event.action, event.payload, _event_ts(event))
        if event.prev_hash != prev or event.row_hash != expected:
            return False, event.id
        prev = event.row_hash
    return True, None
