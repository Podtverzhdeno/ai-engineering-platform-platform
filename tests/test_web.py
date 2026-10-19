from conftest import auth
from tests_common import approved_project_after_interview


def test_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "AI Platform Workspace" in r.text
    paths = client.get("/openapi.json").json()["paths"]
    assert any("interviews" in p for p in paths)


DELTA_MD = """
## ADDED Requirements

### Requirement: Role Change Audit

#### Scenario: Successful change
"""


def test_merge_queue_epic15(client, users):
    pid = approved_project_after_interview(client, users)
    r = client.post("/v1/changes", json={"project_id": pid, "title": "x", "affected_paths": ["docs/readme.md"]}, headers=auth(users, "alice"))
    cid = r.json()["id"]
    client.post(f"/v1/changes/{cid}/delta", json={"delta_md": DELTA_MD}, headers=auth(users, "alice"))
    client.post(
        f"/v1/changes/{cid}/design",
        json={"design_md": "d", "test_plan_md": "tp", "security_md": "s", "threat_model_md": "tm", "operations_md": "restore plan ok"},
        headers=auth(users, "alice"),
    )
    client.post("/v1/approvals", json={"project_id": pid, "purpose": "design", "change_id": cid}, headers=auth(users, "alice"))
    client.post(f"/v1/changes/{cid}/test-results", json={"suite": "unit", "status": "PASS"}, headers=auth(users, "alice"))

    r = client.post(f"/v1/changes/{cid}/merge", headers=auth(users, "alice"))
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "MERGED"
    assert r.json()["combined_sha"].startswith("sha256:")

    r = client.post(f"/v1/changes/{cid}/releases", json={}, headers=auth(users, "alice"))
    assert r.status_code == 201, "release доступен и после merge"
