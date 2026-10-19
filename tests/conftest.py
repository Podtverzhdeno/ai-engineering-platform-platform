import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="aiplatform-tests-")
os.environ["AIPLATFORM_DB_URL"] = "sqlite:///" + _tmp.replace("\\", "/") + "/test.db"

import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine
from app.main import create_app


@pytest.fixture()
def app():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return create_app()


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def users(client):
    ids = {}
    for name in ("alice", "bob"):
        r = client.post("/v1/users", json={"name": name})
        assert r.status_code == 201, r.text
        ids[name] = r.json()["id"]
    return ids


def auth(ids, name):
    return {"X-User-Id": ids[name]}
