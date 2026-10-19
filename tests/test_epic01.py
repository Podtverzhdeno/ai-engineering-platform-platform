from sqlalchemy import text

from app.db import engine
from conftest import auth


def _project(client, users, name="proj-a"):
    r = client.post("/v1/projects", json={"name": name, "mode": "NEW"}, headers=auth(users, "alice"))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_two_projects_created_and_separated(client, users):
    p1 = _project(client, users, "proj-a")
    p2 = _project(client, users, "proj-b")

    mine = client.get("/v1/projects", headers=auth(users, "alice")).json()
    assert sorted(p["id"] for p in mine) == sorted([p1, p2])

    assert client.get(f"/v1/projects/{p1}", headers=auth(users, "alice")).status_code == 200


def test_rbac_no_read_across_projects(client, users):
    p1 = _project(client, users, "proj-a")
    r = client.get(f"/v1/projects/{p1}", headers=auth(users, "bob"))
    assert r.status_code == 403

    listing = client.get("/v1/projects", headers=auth(users, "bob")).json()
    assert all(p["id"] != p1 for p in listing)


def test_rbac_viewer_cannot_manage(client, users):
    p1 = _project(client, users, "proj-a")
    r = client.post(
        f"/v1/projects/{p1}/members",
        json={"user_id": users["bob"], "role": "viewer"},
        headers=auth(users, "alice"),
    )
    assert r.status_code == 201

    r = client.post("/v1/approvals", json={"project_id": p1, "purpose": "coding"}, headers=auth(users, "bob"))
    assert r.status_code == 403

    r = client.post(f"/v1/projects/{p1}/revision", json={"reason": "x"}, headers=auth(users, "bob"))
    assert r.status_code == 403


def test_audit_chain_valid_and_tamper_detected(client, users):
    p1 = _project(client, users, "proj-a")

    data = client.get(f"/v1/projects/{p1}/audit", headers=auth(users, "alice")).json()
    assert data["chain_ok"] is True
    assert any(e["action"] == "project.created" for e in data["events"])

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE audit_events SET payload = json_set(payload, '$.mode', 'HACKED') WHERE project_id = :pid"),
            {"pid": p1},
        )

    data = client.get(f"/v1/projects/{p1}/audit", headers=auth(users, "alice")).json()
    assert data["chain_ok"] is False
    assert data["broken_event_id"] is not None


def test_audit_has_no_mutation_endpoint(client, users):
    _project(client, users, "proj-a")
    paths = client.get("/openapi.json").json()["paths"]
    audit_paths = [p for p in paths if "audit" in p]
    assert audit_paths == ["/v1/projects/{project_id}/audit"]
    methods = {m.upper() for m in paths[audit_paths[0]]}
    assert methods == {"GET"}
