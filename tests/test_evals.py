from conftest import auth, users  # noqa: F401
from tests_common import approved_project_after_interview

DELTA_MD = """
## ADDED Requirements

### Requirement: Orders Endpoint

#### Scenario: Create order
"""


def _change_with_findings(client, users):
    pid = approved_project_after_interview(client, users, name="eval-proj")
    cid = client.post(
        "/v1/changes", json={"project_id": pid, "title": "eval", "affected_paths": ["docs/eval.md"]}, headers=auth(users, "alice")
    ).json()["id"]
    client.post(f"/v1/changes/{cid}/delta", json={"delta_md": DELTA_MD}, headers=auth(users, "alice"))
    client.post(f"/v1/changes/{cid}/design", json={"design_md": "d", "test_plan_md": "tp", "security_md": "s"}, headers=auth(users, "alice"))
    client.post("/v1/approvals", json={"project_id": pid, "purpose": "design", "change_id": cid}, headers=auth(users, "alice"))
    planted = client.post(
        f"/v1/changes/{cid}/findings",
        json={"classification": "planted", "severity": "HIGH", "evidence_uri": "artifact://p1"},
        headers=auth(users, "alice"),
    ).json()["id"]
    real_fp = client.post(
        f"/v1/changes/{cid}/findings",
        json={"classification": "STYLE", "severity": "LOW", "evidence_uri": "artifact://f1"},
        headers=auth(users, "alice"),
    ).json()["id"]
    real_ok = client.post(
        f"/v1/changes/{cid}/findings",
        json={"classification": "AUTHORIZATION_BYPASS", "severity": "HIGH", "evidence_uri": "artifact://r1"},
        headers=auth(users, "alice"),
    ).json()["id"]
    return pid, cid, planted, real_fp, real_ok


def test_manual_eval_report_flow(client, users):
    pid, cid, planted, real_fp, real_ok = _change_with_findings(client, users)
    report = client.post(
        "/v1/evals/reports",
        json={
            "project_id": pid,
            "planted_cases": {"p1": "CONFIRMED", "p2": "CONFIRMED", "p3": "CONFIRMED"},
            "reviewer_results": {"p1": "CONFIRMED", "p2": "CONFIRMED", "p3": "REJECTED", "x1": "CONFIRMED", "x2": "REJECTED"},
        },
        headers=auth(users, "alice"),
    )
    assert report.status_code == 201, report.text
    body = report.json()
    assert abs(body["metrics"]["recall_planted"] - 0.667) < 0.01
    assert abs(body["metrics"]["reviewer_precision"] - 0.667) < 0.01
    got = client.get(f"/v1/evals/reports/{body['id']}", headers=auth(users, "alice"))
    assert got.status_code == 200
    assert got.json()["dataset_version"].startswith("v1-")


def test_live_eval_uses_real_findings(client, users):
    pid, cid, planted, real_fp, real_ok = _change_with_findings(client, users)
    client.post(f"/v1/findings/{planted}/verify", json={"verdict": "CONFIRMED", "reproduction": "pytest::planted"}, headers=auth(users, "alice"))
    client.post(f"/v1/findings/{real_fp}/verify", json={"verdict": "REJECTED", "evidence": {"repro": "false-positive"}}, headers=auth(users, "alice"))
    client.post(f"/v1/findings/{real_ok}/verify", json={"verdict": "CONFIRMED", "reproduction": "pytest::real"}, headers=auth(users, "alice"))

    live = client.post("/v1/evals/live", json={"project_id": pid}, headers=auth(users, "alice"))
    assert live.status_code == 201, live.text
    body = live.json()
    assert body["metrics"]["planted_total"] == 1
    assert body["metrics"]["findings_total"] == 3
    assert body["metrics"]["recall_planted"] == 1.0
    assert abs(body["metrics"]["reviewer_precision"] - 0.5) < 0.001
    assert body["dataset_version"].startswith("live-")

    got = client.get(f"/v1/evals/reports/{body['id']}", headers=auth(users, "alice"))
    assert got.status_code == 200
    assert got.json()["results"]["verdicts"][planted] == "CONFIRMED"


def test_live_eval_without_findings_rejected(client, users):
    pid = approved_project_after_interview(client, users, name="eval-empty")
    r = client.post("/v1/evals/live", json={"project_id": pid}, headers=auth(users, "alice"))
    assert r.status_code == 422
    assert r.json()["detail"] == "no findings audited yet"
