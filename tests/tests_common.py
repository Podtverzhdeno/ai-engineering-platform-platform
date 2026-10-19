"""Shared helpers for acceptance tests (run interview → approve product)."""
from app.db import SessionLocal
from app.models import Project
from conftest import auth


def run_interview_to_pack(client, users, pid, value="ok"):
    itw = client.post("/v1/interviews", json={"project_id": pid}, headers=auth(users, "alice")).json()["interview_id"]
    answered = set()
    while True:
        data = client.get(f"/v1/interviews/{itw}", headers=auth(users, "alice")).json()
        round_q = data["round"]
        fresh = [q for q in round_q if q["id"] not in answered]
        if not fresh:
            break
        batch = [{"question_id": q["id"], "answer": value} for q in fresh]
        r = client.post(f"/v1/interviews/{itw}/answers", json={"answers": batch}, headers=auth(users, "alice"))
        assert r.status_code == 200, r.text
        for a in batch:
            answered.add(a["question_id"])
    pack = client.post(f"/v1/interviews/{itw}/finalize", headers=auth(users, "alice")).json()["approval_pack"]
    return itw, pack


def approved_project_after_interview(client, users, name="p-approved"):
    r = client.post("/v1/projects", json={"name": name}, headers=auth(users, "alice"))
    pid = r.json()["id"]
    run_interview_to_pack(client, users, pid)
    a = client.post("/v1/approvals", json={"project_id": pid, "purpose": "product"}, headers=auth(users, "alice"))
    assert a.status_code == 201, a.text
    assert a.json()["project_state"] == "PRODUCT_APPROVED"
    return pid
