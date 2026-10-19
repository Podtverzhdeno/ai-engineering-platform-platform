import httpx
import pytest
from fastapi.testclient import TestClient  # noqa

import app.agent as agent_mod
from app.agent import Api, Session, handle
from app.main import app

@pytest.fixture()
def agent_api(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_mod, "SESSION_FILE", str(tmp_path / "sess.json"))
    return Api(base="http://testserver", client=TestClient(app))


def test_power_preset_roundtrip(agent_api):
    s = Session()
    out = handle(s, agent_api, "мощность high")
    assert any("мощность: high" in x for x in out)
    out = handle(s, agent_api, "мощность anthropic/claude-sonnet-4-5")
    assert any("модель: anthropic/claude-sonnet-4-5" in x for x in out)
    assert s.model_label() == "anthropic/claude-sonnet-4-5"


def test_llm_brain_intent_used(agent_api, monkeypatch):
    calls = []

    def fake_llm(provider_id, model_id, text):
        calls.append((provider_id, model_id))
        return '{"intent":"analytics","name":"","title":"","task":"","reply":""}'

    monkeypatch.setattr(agent_mod, "llm_call", fake_llm)
    s = Session()
    assert s.power == "max" and s.model_label() == "anthropic/claude-opus-4-5"
    handle(s, agent_api, "создай пользователя vasya")
    monkeypatch.setattr(
        agent_mod, "llm_call", lambda *a, **k: '{"intent":"chat","reply":"Привет! Готов всё собрать."}'
    )
    out = handle(s, agent_api, "привет, как дела")
    assert any("Привет!" in x for x in out)
    assert calls == [("anthropic", "claude-opus-4-5")]


def test_llm_broken_falls_back_to_parser(agent_api, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("model error: invalid x-api-key")

    monkeypatch.setattr(agent_mod, "llm_call", broken)
    s = Session()
    out = handle(s, agent_api, "создай пользователя vasya")
    assert any("usr-" in x for x in out)
    assert s.llm_broken is True
    out = handle(s, agent_api, "создай проект orders")
    assert any("product approval" in x for x in out)


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
