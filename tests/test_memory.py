from conftest import auth, users  # noqa: F401
from tests_common import approved_project_after_interview

DELTA_MD = """
## ADDED Requirements

### Requirement: Orders Endpoint

#### Scenario: Create order
"""


def test_memory_bundle_reflects_project(client, users):
    pid = approved_project_after_interview(client, users)
    cid = client.post(
        "/v1/changes", json={"project_id": pid, "title": "orders", "affected_paths": ["docs/readme.md"]}, headers=auth(users, "alice")
    ).json()["id"]
    client.post(f"/v1/changes/{cid}/delta", json={"delta_md": DELTA_MD}, headers=auth(users, "alice"))
    adv = client.post(f"/v1/changes/{cid}/tech-advisor", json={"layer": "backend", "constraints": ["self-hosted"], "nfr": {"ml_semantics": True}}, headers=auth(users, "alice"))
    assert adv.status_code == 200, adv.text
    adr_id = adv.json()["adr_id"]
    client.post(f"/v1/changes/{cid}/adr-approve", json={"adr_id": adr_id, "chosen": "Python/FastAPI"}, headers=auth(users, "alice"))
    inc = client.post("/v1/incidents", json={"project_id": pid, "impact": "orders down", "traces": []}, headers=auth(users, "alice")).json()["id"]
    client.post(f"/v1/incidents/{inc}/rca", json={"rca": "migration order", "regression_case": {"scenario": "s"}}, headers=auth(users, "alice"))

    mem = client.get(f"/v1/memory/{pid}", headers=auth(users, "alice"))
    assert mem.status_code == 200, mem.text
    body = mem.json()
    assert body["product"]["spec_hash"].startswith("sha256:")
    assert len(body["adr_decisions"]) >= 1
    assert len(body["spec_ledger"]) >= 1
    assert len(body["incident_lessons"]) >= 1
    assert any("write scope" in r for r in body["policy_lessons"])

    prompt = client.get(f"/v1/memory/{pid}/prompt", headers=auth(users, "alice")).json()["prompt"]
    assert "ADR " in prompt
    assert "Правило: не выходить за write scope задачи" in prompt
