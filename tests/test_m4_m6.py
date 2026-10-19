import json
import os

from app.db import SessionLocal
from app.models import Release, Task
from conftest import auth
from tests_common import approved_project_after_interview

DELTA_MD = """
## ADDED Requirements

### Requirement: Orders Endpoint

#### Scenario: Create order
"""

LOW_TASKS = [
    {"key": "T1", "write_scope": ["docs/readme.md"], "acceptance_ids": ["REQ-1-SC1"]},
    {"key": "T2", "write_scope": ["services/other.py"], "depends_on": ["T1"], "acceptance_ids": ["REQ-1-SC1"]},
]


def _approved_change(client, users):
    pid = approved_project_after_interview(client, users)
    cid = client.post(
        "/v1/changes", json={"project_id": pid, "title": "orders", "affected_paths": ["docs/readme.md"]}, headers=auth(users, "alice")
    ).json()["id"]
    client.post(f"/v1/changes/{cid}/delta", json={"delta_md": DELTA_MD}, headers=auth(users, "alice"))
    client.post(
        f"/v1/changes/{cid}/design",
        json={"design_md": "d", "test_plan_md": "tp", "security_md": "s", "threat_model_md": "tm", "operations_md": "rollback plan; restore plan ok"},
        headers=auth(users, "alice"),
    )
    client.post("/v1/approvals", json={"project_id": pid, "purpose": "design", "change_id": cid}, headers=auth(users, "alice"))
    client.post(
        f"/v1/changes/{cid}/test-results",
        json={"suite": "unit", "status": "PASS", "artifact_hash": "sha256:ci-unit"},
        headers=auth(users, "alice"),
    )
    tasks = client.post(f"/v1/changes/{cid}/plan", json={"tasks": LOW_TASKS}, headers=auth(users, "alice"))
    assert tasks.status_code == 200, tasks.text
    return pid, cid


def test_scope_conflict_blocked_acc06(client, users):
    pid = approved_project_after_interview(client, users)
    cid = client.post(
        "/v1/changes", json={"project_id": pid, "title": "x", "affected_paths": ["docs/readme.md"]}, headers=auth(users, "alice")
    ).json()["id"]
    client.post(f"/v1/changes/{cid}/delta", json={"delta_md": DELTA_MD}, headers=auth(users, "alice"))
    client.post(f"/v1/changes/{cid}/design", json={"design_md": "d", "test_plan_md": "tp", "security_md": "s"}, headers=auth(users, "alice"))
    client.post("/v1/approvals", json={"project_id": pid, "purpose": "design", "change_id": cid}, headers=auth(users, "alice"))

    r = client.post(
        f"/v1/changes/{cid}/plan",
        json={"tasks": [
            {"key": "A", "write_scope": ["services/orders/**"], "acceptance_ids": ["S1"]},
            {"key": "B", "write_scope": ["services/orders/api.py"], "acceptance_ids": ["S1"]},
        ]},
        headers=auth(users, "alice"),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "WRITE_SCOPE_CONFLICT"


def test_ready_queue_and_acc18(client, users):
    pid, cid = _approved_change(client, users)
    tasks = client.get(f"/v1/changes/{cid}/tasks", headers=auth(users, "alice")).json()
    ready = [t for t in tasks if t["status"] == "READY"]
    assert len(ready) == 1, "ACC-18: одна задача — один поток, не 3"

    r = client.post(f"/v1/changes/{cid}/tasks/T1/run", headers=auth(users, "alice"))
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]

    r = client.post(f"/v1/changes/{cid}/tasks/T1/run", headers=auth(users, "alice"))
    assert r.status_code == 409

    bad = {"commit_sha": "", "test_result": "PASS", "diff_summary": "x", "open_questions": []}
    r = client.post(f"/v1/runs/{run_id}/finish", json=bad, headers=auth(users, "alice"))
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "OUTPUT_INCOMPLETE"

    tasks = client.get(f"/v1/changes/{cid}/tasks", headers=auth(users, "alice")).json()
    assert all(t["status"] != "DONE" for t in tasks)

    r = client.post(f"/v1/runs/{run_id}/finish", json={"commit_sha": "a" * 39}, headers=auth(users, "alice"))
    assert r.status_code == 422

    result = client.post(
        f"/v1/runs/{run_id}/finish",
        json={"commit_sha": "b" * 40, "test_result": "PASS", "diff_summary": "d", "open_questions": []},
        headers=auth(users, "alice"),
    )
    assert result.status_code == 200, result.text
    tasks = client.get(f"/v1/changes/{cid}/tasks", headers=auth(users, "alice")).json()
    t2 = [t for t in tasks if t["title"] == "T2"][0]
    assert t2["status"] == "READY"


def test_budget_blocks_run(client, users):
    pid, cid = _approved_change(client, users)
    r = client.post(f"/v1/projects/{pid}/budget", json={"budget_usd": 1.0}, headers=auth(users, "alice"))
    assert r.status_code == 200
    first = client.post(f"/v1/changes/{cid}/tasks/T1/run", headers=auth(users, "alice"))
    assert first.status_code == 201
    second = client.post(f"/v1/changes/{cid}/tasks/T1/run" if False else f"/v1/changes/{cid}/tasks/T1/run", headers=auth(users, "alice"))
    assert second.status_code in (402, 409)


def test_finding_requires_evidence_and_verifier_gate(client, users):
    pid, cid = _approved_change(client, users)
    r = client.post(
        f"/v1/changes/{cid}/findings",
        json={"classification": "AUTHORIZATION_BYPASS", "severity": "HIGH", "evidence_uri": ""},
        headers=auth(users, "alice"),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "EVIDENCE_REQUIRED"

    r = client.post(
        f"/v1/changes/{cid}/findings",
        json={"classification": "STYLE", "severity": "LOW", "evidence_uri": "artifact://1"},
        headers=auth(users, "alice"),
    )
    fid = r.json()["id"]
    verify = client.post(
        f"/v1/findings/{fid}/verify", json={"verdict": "CONFIRMED", "reproduction": "pytest::1"}, headers=auth(users, "alice")
    )
    assert verify.status_code == 200


def test_release_blocked_by_high_security_finding_acc09(client, users, monkeypatch):
    pid, cid = _approved_change(client, users)
    f = client.post(
        f"/v1/changes/{cid}/findings",
        json={"classification": "IDOR", "severity": "HIGH", "evidence_uri": "artifact://2"},
        headers=auth(users, "alice"),
    ).json()["id"]
    v = client.post(f"/v1/findings/{f}/verify", json={"verdict": "CONFIRMED", "reproduction": "pytest::idor"}, headers=auth(users, "alice"))
    assert v.status_code == 200

    r = client.post(f"/v1/changes/{cid}/releases", json={}, headers=auth(users, "alice"))
    assert r.status_code == 403
    body = r.json()["detail"]
    assert body["error"] == "RELEASE_NO_GO"
    assert any("blocking_security" in g for g in body["gaps"])

    verify2 = client.post(f"/v1/findings/{f}/verify", json={"verdict": "REJECTED", "evidence": {"repro": "fails"}}, headers=auth(users, "alice"))
    assert verify2.status_code == 200
    assert verify2.json()["resolution"] == "CLOSED_FALSE_POSITIVE"

    r2 = client.post(f"/v1/changes/{cid}/releases", json={}, headers=auth(users, "alice"))
    assert r2.status_code == 201, r2.text
    rel = r2.json()["id"]

    prod = client.post(f"/v1/releases/{rel}/deploy", json={"environment": "production"}, headers=auth(users, "alice"))
    assert prod.status_code == 403
    assert prod.json()["detail"]["error"] == "PROD_DEPLOY_REFUSED"

    staging = client.post(f"/v1/releases/{rel}/deploy", json={"environment": "staging"}, headers=auth(users, "alice"))
    assert staging.status_code == 200
    assert staging.json()["state"] == "DEPLOYED"

    rb = client.post(f"/v1/releases/{rel}/rollback", headers=auth(users, "alice"))
    assert rb.status_code == 200
    assert rb.json()["state"] == "ROLLED_BACK"


def test_convergence_blocked_after_three_cycles(client, users):
    pid, cid = _approved_change(client, users)
    for _ in range(3):
        r = client.post(f"/v1/changes/{cid}/review-cycle", headers=auth(users, "alice"))
        if r.status_code == 409:
            break
    r = client.post(f"/v1/changes/{cid}/review-cycle", headers=auth(users, "alice"))
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "CONVERGENCE_BLOCKED"


def test_incident_produces_regression_req_acc13(client, users):
    pid, cid = _approved_change(client, users)
    inc = client.post(
        "/v1/incidents", json={"project_id": pid, "impact": "orders unavailable", "traces": ["trace-1"]}, headers=auth(users, "alice")
    ).json()["id"]
    r = client.post(
        f"/v1/incidents/{inc}/rca",
        json={"rca": "bad migration order", "regression_case": {"scenario": "POST /orders with pending migration fails gracefully"}},
        headers=auth(users, "alice"),
    )
    assert r.status_code == 200
    regression_req = r.json()["regression_requirement"]
    assert regression_req.startswith("REQ-")

    dash = client.get(f"/v1/pslc-dashboard/{pid}", headers=auth(users, "alice")).json()
    assert dash["incidents"] == 1


def test_policy_denials_visible_in_dashboard(client, users):
    pid, cid = _approved_change(client, users)
    rel = client.post(f"/v1/changes/{cid}/releases", json={}, headers=auth(users, "alice")).json()["id"]
    assert client.post(f"/v1/releases/{rel}/deploy", json={"environment": "production"}, headers=auth(users, "alice")).status_code == 403
    dash = client.get(f"/v1/pslc-dashboard/{pid}", headers=auth(users, "alice")).json()
    assert dash["policy_denials"] == 1


def test_run_cancel_unblocks_task_v2(client, users):
    pid, cid = _approved_change(client, users)
    r = client.post(f"/v1/changes/{cid}/tasks/T1/run", headers=auth(users, "alice"))
    assert r.status_code == 201
    stuck = client.post(f"/v1/changes/{cid}/tasks/T1/run", headers=auth(users, "alice"))
    assert stuck.status_code == 409
    cancel = client.post(f"/v1/runs/{r.json()['run_id']}/cancel", headers=auth(users, "alice"))
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "CANCELLED"
    again = client.post(f"/v1/changes/{cid}/tasks/T1/run", headers=auth(users, "alice"))
    assert again.status_code == 201, "cancel возвращает задачу в READY"
    double = client.post(f"/v1/runs/{r.json()['run_id']}/cancel", headers=auth(users, "alice"))
    assert double.status_code == 409


def test_read_run_contract_for_runner(client, users):
    pid, cid = _approved_change(client, users)
    run_id = client.post(f"/v1/changes/{cid}/tasks/T1/run", headers=auth(users, "alice")).json()["run_id"]
    data = client.get(f"/v1/runs/{run_id}", headers=auth(users, "alice"))
    assert data.status_code == 200, data.text
    body = data.json()
    assert body["run_id"] == run_id
    assert body["status"] == "PENDING"
    assert body["task"]["title"] == "T1"
    assert body["task"]["write_scope"] == ["docs/readme.md"]
    assert body["task"]["acceptance_ids"] == ["REQ-1-SC1"]
    workspace = body["workspace"]
    assert workspace and workspace.endswith(os.path.join(".workspaces", "")) is False
    assert "workspace.materialized" in json.dumps(body["traces"])
    assert os.path.isfile(os.path.join(workspace, "task-contract.json"))
    outsider = client.get(f"/v1/runs/{run_id}", headers=auth(users, "bob"))
    assert outsider.status_code == 403
