import httpx
import pytest
from fastapi.testclient import TestClient  # noqa

from app.agent import Api, Session, handle
from app.main import app

@pytest.fixture()
def agent_api():
    return Api(base="http://testserver", client=TestClient(app))


def _reply(api, user, path, body=None):
    r = api.client.request("POST", "http://testserver" + path, json=body, headers={"X-User-Id": user} if user else {})
    assert r.status_code < 500


def test_chat_help_and_unknown(agent_api):
    s = Session()
    out = handle(s, agent_api, "помощь")
    assert any("создай проект" in m for m in out)
    out = handle(s, agent_api, "яяя бессмыслица")
    assert "Не понял фразы." in out[0]


def test_chat_full_flow_user_project_change(agent_api, tmp_path, monkeypatch):
    monkeypatch.setattr("app.agent.SESSION_FILE", str(tmp_path / "sess.json"))
    s = Session()
    out = handle(s, agent_api, "создай пользователя vasya")
    assert any("usr-" in x for x in out)
    assert s.uid.startswith("usr-")

    out = handle(s, agent_api, "создай проект orders")
    assert any("product approval" in x for x in out)
    assert s.current_project.startswith("PRJ-")
    assert any("создай change" in x for x in out)

    pid = s.current_project
    out = handle(s, agent_api, "статус")
    assert any(f"проект: {pid}" in m for m in out)

    out = handle(s, agent_api, "создай change каталог")
    assert any("change:" in m for m in out)
    assert s.current_change.startswith("CHG-")

    out = handle(s, agent_api, "мёрж")
    assert any("merge: " in m for m in out)

    out = handle(s, agent_api, "аналитика")
    assert any("аналитика" in m for m in out)

    s.save() and None
    s2 = Session.load() if False else None
