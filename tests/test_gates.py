import datetime as dt

from app.db import SessionLocal
from app.models import Job, Project, utcnow
from app.policy import risk_floor
from app.services import workflow
from conftest import auth


def _project(client, users):
    r = client.post("/v1/projects", json={"name": "p", "mode": "NEW"}, headers=auth(users, "alice"))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _force_state(project_id: str, state: str) -> None:
    with SessionLocal() as db:
        db.get(Project, project_id).state = state
        db.commit()


def test_coding_denied_without_product_approval(client, users):
    pid = _project(client, users)
    r = client.post("/v1/coding-sessions", json={"project_id": pid}, headers=auth(users, "alice"))
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["error"] == "PRODUCT_NOT_APPROVED"
    assert body["required_event"] == "PRODUCT_APPROVED"
    assert "missing" in body


def test_coding_denied_with_state_but_missing_signature(client, users):
    pid = _project(client, users)
    _force_state(pid, "PRODUCT_APPROVED")
    r = client.post("/v1/coding-sessions", json={"project_id": pid}, headers=auth(users, "alice"))
    assert r.status_code == 403
    assert r.json()["error"] == "MISSING_PRODUCT_APPROVAL"


def test_coding_allowed_after_bound_approval(client, users):
    pid = _project(client, users)
    _force_state(pid, "PRODUCT_APPROVED")

    a = client.post("/v1/approvals", json={"project_id": pid, "purpose": "coding"}, headers=auth(users, "alice"))
    assert a.status_code == 201, a.text
    spec_hash = a.json()["spec_hash"]
    assert spec_hash.startswith("sha256:spec-")

    r = client.post("/v1/coding-sessions", json={"project_id": pid}, headers=auth(users, "alice"))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["queued_new"] is True
    assert body["spec_hash"] == spec_hash


def test_revision_invalidates_old_approval_acc03(client, users):
    pid = _project(client, users)
    _force_state(pid, "PRODUCT_APPROVED")

    a1 = client.post("/v1/approvals", json={"project_id": pid, "purpose": "coding"}, headers=auth(users, "alice")).json()
    assert client.post("/v1/coding-sessions", json={"project_id": pid}, headers=auth(users, "alice")).status_code == 202

    r = client.post(f"/v1/projects/{pid}/revision", json={"reason": "scope change"}, headers=auth(users, "alice"))
    assert r.status_code == 200
    assert r.json()["invalidated_approvals"] == 1

    approvals = client.get(f"/v1/projects/{pid}/approvals", headers=auth(users, "alice")).json()
    assert approvals[0]["status"] == "STALE"

    r = client.post("/v1/coding-sessions", json={"project_id": pid}, headers=auth(users, "alice"))
    assert r.status_code == 403
    assert r.json()["error"] == "MISSING_PRODUCT_APPROVAL"


def test_policy_floor_cannot_be_lowered():
    assert risk_floor(["app/policy.py"], ["feature"]) == "CRITICAL"
    assert risk_floor(["services/x.py"], ["auth_permissions"]) == "HIGH"
    assert risk_floor(["services/x.py"], ["prod_migration"]) == "HIGH"
    assert risk_floor(["services/x.py"], ["feature"]) == "MEDIUM"
    assert risk_floor(["services/x.py"], ["docs"]) == "LOW"
    assert risk_floor(["services/x.py"], ["disallowed_llm_provider"]) == "DENY"


def called_effect(db, job):
    job.state = {"external_calls": int(job.state.get("external_calls", 0)) + 1}
    db.flush()


def test_workflow_idempotent_enqueue():
    with SessionLocal() as db:
        j1, c1 = workflow.enqueue(db, "nop", {"x": 1}, idempotency_key="key-1")
        j2, c2 = workflow.enqueue(db, "nop", {"x": 1}, idempotency_key="key-1")
        db.commit()
        assert c1 is True and c2 is False
        assert j1.id == j2.id


def test_workflow_recovery_no_duplicate_effects():
    workflow.register("probe", called_effect)
    with SessionLocal() as db:
        from app.api.coding import _start_opencode_session

        workflow.register("opencode.start-session", _start_opencode_session)
        for _ in range(50):
            pending = workflow.claim_next(db)
            if pending is None:
                break
            workflow.step(db, pending)
        db.commit()

        job, _ = workflow.enqueue(db, "probe", {}, idempotency_key="recovery-1")
        db.commit()

        claimed = workflow.claim_next(db)
        assert claimed.id == job.id
        db.commit()

        claimed.lease_until = utcnow() - dt.timedelta(seconds=1)
        db.commit()

        assert workflow.recover_expired(db) == 1
        db.commit()

        again = workflow.claim_next(db)
        assert again.id == job.id
        workflow.step(db, again)
        db.commit()

        final = db.query(Job).filter(Job.idempotency_key == "recovery-1").one()
        assert final.status == "DONE"
        assert final.state["external_calls"] == 1
