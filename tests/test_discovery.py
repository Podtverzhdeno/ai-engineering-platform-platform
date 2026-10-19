from app.db import SessionLocal
from app.models import Project
from conftest import auth


def make_project(client, users, state="CREATED"):
    r = client.post("/v1/projects", json={"name": "p-discovery", "mode": "NEW"}, headers=auth(users, "alice"))
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    if state != "CREATED":
        with SessionLocal() as db:
            db.get(Project, pid).state = state
            db.commit()
    return pid


def answer_full_round(answers, round_questions, value="ok"):
    for q in round_questions:
        answers.append({"question_id": q["id"], "answer": value})


def test_intake_transitions_state(client, users):
    pid = make_project(client, users)
    r = client.post("/v1/interviews", json={"project_id": pid}, headers=auth(users, "alice"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["state"] == "INTERVIEW_IN_PROGRESS"
    assert 5 <= len(body["round"]) <= 8
    assert client.post("/v1/interviews", json={"project_id": pid}, headers=auth(users, "alice")).status_code == 409


def test_provenance_model_suggestion_not_confirmed(client, users):
    pid = make_project(client, users)
    itw = client.post("/v1/interviews", json={"project_id": pid}, headers=auth(users, "alice")).json()["interview_id"]
    r = client.post(f"/v1/interviews/{itw}/suggest", json={"feature_text": "AI-summary of audit log"}, headers=auth(users, "alice"))
    assert r.status_code == 201
    data = client.get(f"/v1/interviews/{itw}", headers=auth(users, "alice")).json()
    assert data["ledger"]["suggestions"][0]["source"] == "MODEL_SUGGESTED"
    assert data["ledger"]["suggestions"][0]["status"] == "OPEN"
    assert all(a["source"] != "MODEL_SUGGESTED" for a in data["ledger"]["confirmed"])


def test_finalize_blocked_by_critical_gaps(client, users):
    pid = make_project(client, users)
    itw = client.post("/v1/interviews", json={"project_id": pid}, headers=auth(users, "alice")).json()["interview_id"]
    r = client.post(f"/v1/interviews/{itw}/finalize", headers=auth(users, "alice"))
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "CRITICAL_GAPS_OPEN"
    gaps = client.get(f"/v1/interviews/{itw}/gaps", headers=auth(users, "alice")).json()["gaps"]
    assert any(g["area"] == "problem" for g in gaps)


def test_finalize_creates_requirements_and_pack(client, users):
    pid = make_project(client, users)
    itw = client.post("/v1/interviews", json={"project_id": pid}, headers=auth(users, "alice")).json()["interview_id"]

    answers = []
    while True:
        round = client.get(f"/v1/interviews/{itw}", headers=auth(users, "alice")).json()["round"]
        if not round:
            break
        answer_full_round(answers, round)
        client.post(f"/v1/interviews/{itw}/answers", json={"answers": answers[-len(round):]}, headers=auth(users, "alice"))

    pack = client.post(f"/v1/interviews/{itw}/finalize", headers=auth(users, "alice")).json()["approval_pack"]
    assert pack["approval_status"] == "PENDING_USER"
    assert pack["immutable_spec_hash"].startswith("sha256:")

    r = client.post("/v1/approvals", json={"project_id": pid, "purpose": "product"}, headers=auth(users, "alice"))
    assert r.status_code == 201, r.text
    assert r.json()["project_state"] == "PRODUCT_APPROVED"


def test_identical_answers_two_projects_no_id_collision(client, users):
    def full_flow(name: str) -> None:
        r = client.post("/v1/projects", json={"name": name, "mode": "NEW"}, headers=auth(users, "alice"))
        pid = r.json()["id"]
        itw = client.post("/v1/interviews", json={"project_id": pid}, headers=auth(users, "alice")).json()["interview_id"]
        answered: set[str] = set()
        for _ in range(8):
            round = client.get(f"/v1/interviews/{itw}", headers=auth(users, "alice")).json()["round"]
            fresh = [q for q in round if q["id"] not in answered]
            if not fresh:
                break
            batch = [{"question_id": q["id"], "answer": "Значение: 30%. Затрагивается: docs/x.md. Интеграция: нет."} for q in fresh]
            client.post(f"/v1/interviews/{itw}/answers", json={"answers": batch}, headers=auth(users, "alice"))
            answered.update(q["id"] for q in fresh)
        fin = client.post(f"/v1/interviews/{itw}/finalize", headers=auth(users, "alice"))
        assert fin.status_code == 200, fin.text
        ap = client.post("/v1/approvals", json={"project_id": pid, "purpose": "product"}, headers=auth(users, "alice"))
        assert ap.json()["project_state"] == "PRODUCT_APPROVED", ap.text

    full_flow("dup-same-answers-1")
    full_flow("dup-same-answers-2")
