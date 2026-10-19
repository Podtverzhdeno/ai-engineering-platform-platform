import datetime as dt

from sqlalchemy.orm import Session

from app.config import lease_seconds
from app.models import Job, utcnow

Registry: dict[str, callable] = {}


def register(kind: str, fn: callable) -> None:
    Registry[kind] = fn


def enqueue(db: Session, kind: str, payload: dict, idempotency_key: str, max_attempts: int = 3) -> tuple[Job, bool]:
    existing = db.query(Job).filter(Job.idempotency_key == idempotency_key).one_or_none()
    if existing is not None:
        return existing, False
    job = Job(
        id=f"job-{idempotency_key[:10]}-{job_seq(db)}",
        kind=kind,
        idempotency_key=idempotency_key,
        payload=payload,
        max_attempts=max_attempts,
    )
    db.add(job)
    db.flush()
    return job, True


def job_seq(db: Session) -> int:
    return db.query(Job).count() + 1


def claim_next(db: Session) -> Job | None:
    now = utcnow()
    job = db.query(Job).filter(Job.status == "RUNNABLE").order_by(Job.created_at).first()
    if job is None:
        job = (
            db.query(Job)
            .filter(Job.status == "RUNNING", Job.lease_until != None, Job.lease_until < now)
            .first()
        )
    if job is None:
        return None
    job.status = "RUNNING"
    job.lease_until = now + dt.timedelta(seconds=lease_seconds())
    db.flush()
    return job


def step(db: Session, job: Job) -> Job:
    fn = Registry.get(job.kind)
    try:
        if fn is None:
            raise RuntimeError(f"no handler for kind {job.kind}")
        fn(db, job)
        job.status = "DONE"
        job.lease_until = None
        job.last_error = ""
    except Exception as exc:
        job.attempts += 1
        job.last_error = str(exc)[:2000]
        if job.attempts >= job.max_attempts:
            job.status = "FAILED"
            job.lease_until = None
        else:
            job.status = "RUNNABLE"
            job.lease_until = None
    db.flush()
    return job


def recover_expired(db: Session) -> int:
    now = utcnow()
    expired = db.query(Job).filter(Job.status == "RUNNING", Job.lease_until != None, Job.lease_until < now).all()
    for job in expired:
        job.status = "RUNNABLE"
        job.lease_until = None
    if expired:
        db.flush()
    return len(expired)


def run_pending(db: Session) -> int:
    count = 0
    while True:
        job = claim_next(db)
        if job is None:
            break
        step(db, job)
        count += 1
        if job.status == "RUNNABLE":
            break
    return count
