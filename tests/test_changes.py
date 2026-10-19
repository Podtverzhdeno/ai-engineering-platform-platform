from app.db import SessionLocal
from app.models import Project
from conftest import auth
from tests_common import approved_project_after_interview

DELTA_MD = """
# Change: add audit events to role changes

## ADDED Requirements

### Requirement: Role Change Audit

#### Scenario: Successful change
"""


def test_change_requires_product_approval(client, users):
    pid = approved_project_after_interview(client, users)
    r = client.post(
        "/v1/changes",
        json={"project_id": pid, "title": "audit events", "affected_paths": ["services/audit.py"]},
        headers=auth(users, "alice"),
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    pid2 = client.post("/v1/projects", json={"name": "too-early"}, headers=auth(users, "bob")).json()["id"]
    r = client.post("/v1/changes", json={"project_id": pid2, "title": "x"}, headers=auth(users, "bob"))
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "PRODUCT_NOT_APPROVED"


def test_delta_submission_and_hash_invalidation(client, users):
    pid = approved_project_after_interview(client, users)
    cid = client.post(
        "/v1/changes", json={"project_id": pid, "title": "audit", "affected_paths": ["services/audit.py"]}, headers=auth(users, "alice")
    ).json()["id"]

    r = client.post(f"/v1/changes/{cid}/delta", json={"delta_md": "garbage without sections"}, headers=auth(users, "alice"))
    assert r.status_code == 422

    r = client.post(f"/v1/changes/{cid}/delta", json={"delta_md": DELTA_MD}, headers=auth(users, "alice"))
    assert r.status_code == 200, r.text
    spec_hash = r.json()["spec_hash"]
    assert spec_hash.startswith("sha256:")
    assert r.json()["delta"]["ADDED"][0]["name"] == "Role Change Audit"

    r = client.post(f"/v1/changes/{cid}/delta", json={"delta_md": DELTA_MD.strip() + "\n#### Scenario: other"}, headers=auth(users, "alice"))
    assert r.json()["spec_hash"] != spec_hash


def test_design_gate_low_risk_auto_approve_and_execute(client, users):
    pid = approved_project_after_interview(client, users)
    cid = client.post(
        "/v1/changes", json={"project_id": pid, "title": "small", "affected_paths": ["docs/readme.md"]}, headers=auth(users, "alice")
    ).json()["id"]
    client.post(f"/v1/changes/{cid}/delta", json={"delta_md": DELTA_MD}, headers=auth(users, "alice"))

    r = client.post(
        f"/v1/changes/{cid}/design",
        json={"design_md": "simple design", "test_plan_md": "tp", "security_md": "sec", "threat_model_md": "tm"},
        headers=auth(users, "alice"),
    )
    assert r.status_code == 200, r.text

    a = client.post(
        "/v1/approvals", json={"project_id": pid, "purpose": "design", "change_id": cid}, headers=auth(users, "alice")
    ).json()
    assert a["change_state"] == "DESIGN_APPROVED"

    r = client.post(f"/v1/changes/{cid}/execute", headers=auth(users, "alice"))
    assert r.status_code == 202, r.text
    assert r.json()["queued_new"] is True


def test_design_gate_high_risk_blocks_single_user_acc09(client, users):
    pid = approved_project_after_interview(client, users)
    cid = client.post(
        "/v1/changes",
        json={"project_id": pid, "title": "auth change", "scope_types": ["auth_permissions"], "affected_paths": ["services/auth.py"]},
        headers=auth(users, "alice"),
    ).json()["id"]
    client.post(f"/v1/changes/{cid}/delta", json={"delta_md": DELTA_MD}, headers=auth(users, "alice"))
    client.post(
        f"/v1/changes/{cid}/design", json={"design_md": "auth redesign", "test_plan_md": "tp", "security_md": "sec"},
        headers=auth(users, "alice"),
    )

    r = client.post("/v1/approvals", json={"project_id": pid, "purpose": "design", "change_id": cid}, headers=auth(users, "alice"))
    assert r.status_code == 201
    change = client.get(f"/v1/changes/{cid}", headers=auth(users, "alice")).json()
    assert change["state"] == "DESIGNING", "HIGH risk требует 2 approvals — single-user остаётся DESIGNING"

    r = client.post(f"/v1/changes/{cid}/execute", headers=auth(users, "alice"))
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "DESIGN_GATE_BLOCKED"


def test_tech_advisor_acc01_acc17(client, users):
    pid = approved_project_after_interview(client, users)
    cid = client.post("/v1/changes", json={"project_id": pid, "title": "t", "affected_paths": ["services/x.py"]}, headers=auth(users, "alice")).json()["id"]

    r = client.post(f"/v1/changes/{cid}/tech-advisor", json={"layer": "backend", "constraints": ["self-hosted", "Python engineers"]}, headers=auth(users, "alice"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["alternatives"]).isdisjoint({body["recommended"]})
    assert len(body["alternatives"]) >= 1

    r = client.post(
        f"/v1/changes/{cid}/adr-approve",
        json={"adr_id": body["adr_id"], "chosen": "TypeScript/NestJS"},
        headers=auth(users, "alice"),
    )
    assert r.status_code == 200
    assert "TypeScript/NestJS" in r.json()["decision"]
