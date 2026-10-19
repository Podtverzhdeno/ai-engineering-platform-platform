import httpx
import pytest
from fastapi.testclient import TestClient  # noqa

import app.agent as agent_mod
from app.agent import Api, Session, handle
from app.main import app

@pytest.fixture()
def agent_api(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_mod, "SESSION_FILE", str(tmp_path / "sess.json"))
    monkeypatch.setattr(agent_mod, "provider_variants", lambda: {})
    monkeypatch.setenv("AIPLATFORM_CHAT_BRAIN", "off")
    return Api(base="http://testserver", client=TestClient(app))


def test_power_preset_roundtrip(agent_api):
    s = Session()
    out = handle(s, agent_api, "мощность high")
    assert any("мощность: high" in x for x in out)
    out = handle(s, agent_api, "мощность anthropic/claude-sonnet-4-5")
    assert any("модель: anthropic/claude-sonnet-4-5" in x for x in out)
    assert s.model_label() == "anthropic/claude-sonnet-4-5"


def test_llm_brain_intent_used(agent_api, monkeypatch):
    monkeypatch.setenv("AIPLATFORM_CHAT_BRAIN", "on")
    calls = []

    def fake_llm(provider_id, model_id, text, variant="", session_id=""):
        calls.append((provider_id, model_id))
        if "привет" in text:
            return '{"intent":"chat","reply":"Привет! Готов всё собрать."}', "ses_2"
        return '{"intent":"user","name":"vasya","title":"","task":"","reply":""}', "ses_1"

    monkeypatch.setattr(agent_mod, "llm_call", fake_llm)
    s = Session()
    assert s.power == "max"
    out = handle(s, agent_api, "создай пользователя vasya")
    assert any("usr-" in x for x in out)
    out = handle(s, agent_api, "привет, как дела")
    assert any("Привет!" in x for x in out)
    assert calls and calls[0][0] == "anthropic" and calls[0][1] == "claude-opus-4-5"

def test_llm_broken_falls_back_to_parser(agent_api, monkeypatch):
    monkeypatch.setenv("AIPLATFORM_CHAT_BRAIN", "on")

    def broken(*a, **k):
        raise RuntimeError("invalid x-api-key")

    monkeypatch.setattr(agent_mod, "llm_call", broken)
    s = Session()
    out = handle(s, agent_api, "создай пользователя vasya")
    assert any("usr-" in x for x in out)
    assert s.llm_broken is True
    out = handle(s, agent_api, "создай проект orders")
    assert any("product approval" in x for x in out)


def test_free_provider_choice(agent_api, monkeypatch):
    monkeypatch.setattr(agent_mod, "provider_variants", lambda: {})
    s = Session()
    out = handle(s, agent_api, "провайдер opencode")
    assert any("провайдер opencode" in x and "glm-5.3-flash-free" in x for x in out)
    out = handle(s, agent_api, "мышление low")
    assert any("glm-5.3-flash-free, размышление: low" in x for x in out)


def test_stale_context_heals(agent_api):
    s = Session()
    s.uid = "usr-deadbeefdead"
    s.uname = "owner"
    s.projects = {}
    s.current_project = "PRJ-ghost"
    s.current_change = ""
    s.save()
    out = handle(s, agent_api, "создай change каталог цен")
    assert any("сессия с другого сервера" in x for x in out)
    assert s.uid.startswith("usr-") and s.uid != "usr-deadbeefdead"
    assert s.current_project.startswith("PRJ-")
    assert s.current_change.startswith("CHG-")


def test_run_without_change_autocreates_chain(agent_api):
    s = Session()
    out = handle(s, agent_api, "запусти задачу")
    assert any("нет change" in x for x in out)
    assert s.current_change.startswith("CHG-")
    assert s.current_project.startswith("PRJ-")


def test_imperative_question_executes(agent_api, monkeypatch):
    monkeypatch.setenv("AIPLATFORM_CHAT_BRAIN", "on")

    def fake(provider_id, model_id, text, variant="", session_id=""):
        return '{"intent":"chat","reply":"Могу попробовать.","name":"","title":"","task":""}', "s9"

    monkeypatch.setattr(agent_mod, "llm_call", fake)
    s = Session()
    out = handle(s, agent_api, "можешь внести фичи для улучшений каталога?")
    assert s.current_change.startswith("CHG-")
    assert any("change" in x for x in out)


def test_project_reuse(agent_api):
    s = Session()
    handle(s, agent_api, "создай проект orders")
    first = s.current_project
    out = handle(s, agent_api, "можешь улучшить проект orders?")
    assert first == s.current_project
    assert len(s.projects) == 1
    assert s.current_change.startswith("CHG-")
    assert any("change" in x for x in out)


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
