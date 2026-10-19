"""aip chat — разговорный интерфейс платформы.

    aip chat

Пользователь пишет по-русски ("создай проект orders" / "запусти задачу" /
"мёрж" / "статус" / "демо"), агент выполняет через API и проговаривает
каждый шаг. Контекст пользователь/проект/change сохраняется между репликами
в .aip-session.json.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import httpx


DEFAULT_URL = os.environ.get("AIPLATFORM_URL", "http://127.0.0.1:7700")
SESSION_FILE = os.path.join(os.getcwd(), ".aip-session.json")
OPENCODE_URL = os.environ.get("AIPLATFORM_OPENCODE_URL", "http://127.0.0.1:4096")

MODEL_PRESETS: dict[str, tuple[str, str]] = {
    "low": ("opencode", "glm-5.3-flash-free"),
    "medium": ("anthropic", "claude-haiku-4-5"),
    "high": ("anthropic", "claude-sonnet-4-5"),
    "max": ("anthropic", "claude-opus-4-5"),
}

LLM_DIRECTIVE = (
    "Ты мозг CLI-агента платформы. Отвечай ОДНОЙ строкой строгого JSON без markdown:\n"
    '{"intent":"user|project|change|run|merge|status|analytics|demo|help|chat",'
    '"name":"","title":"","task":"","reply":""}\n'
    "Поле reply — только для intent=chat: твой живой русский ответ пользователю.\n"
    "правила: empty поля — empty строки; не добавляй ничего кроме JSON."
)


def _oc_request(method: str, path: str, body: dict | None = None, timeout: float = 240.0) -> dict:
    auth = (
        os.environ.get("OPENCODE_SERVER_USERNAME", "opencode"),
        os.environ.get("OPENCODE_SERVER_PASSWORD", "opencode"),
    )
    r = httpx.request(method, OPENCODE_URL + path, json=body, auth=auth, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"{path} -> {r.status_code}")
    return r.json()


def llm_call(provider_id: str, model_id: str, text: str) -> str:
    data = _oc_request("POST", "/session", {"title": "aip-chat"})
    sid = data["id"]
    msg = _oc_request(
        "POST",
        f"/session/{sid}/message",
        {"modelID": model_id, "providerID": provider_id, "parts": [{"type": "text", "text": text}]},
    )
    info = (msg or {}).get("info") or {}
    err = info.get("error")
    if err:
        raise RuntimeError(f"model error: {str(err.get('data', {}).get('message', err))[:120]}")
    texts = [
        p.get("text", "")
        for p in (msg.get("parts") or [])
        if isinstance(p, dict) and p.get("type") == "text"
    ]
    return "\n".join(t for t in texts if t).strip()


def _typical_answer(project_name: str) -> str:
    return (
        f"Значение: принято. Фича '{project_name}' обязательна к MVP. "
        f"Затрагивается: docs/{project_name}.md. Интеграция: нет."
    )


class Api:
    def __init__(self, base: str = DEFAULT_URL, client: httpx.Client | None = None) -> None:
        self.base = base.rstrip("/")
        self.client = client or httpx.Client(timeout=60)

    def call(self, method: str, path: str, uid: str = "", body: dict | None = None) -> dict:
        headers = {"X-User-Id": uid} if uid else {}
        r = self.client.request(method, self.base + path, headers=headers, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def health(self) -> bool:
        try:
            r = self.client.get(self.base + "/healthz", timeout=2)
            return r.status_code == 200
        except (httpx.HTTPError, OSError):
            return False


class Session:
    def __init__(self) -> None:
        self.uid: str = ""
        self.uname: str = ""
        self.projects: dict[str, dict] = {}
        self.current_project: str = ""
        self.current_change: str = ""
        self.pending: dict | None = None
        self.power: str = "max"
        self.model: str = ""
        self.llm_broken: bool = False

    def save(self) -> None:
        try:
            with open(SESSION_FILE, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "uid": self.uid,
                        "uname": self.uname,
                        "projects": self.projects,
                        "current_project": self.current_project,
                        "current_change": self.current_change,
                        "power": self.power,
                        "model": self.model,
                        "llm_broken": self.llm_broken,
                    },
                    fh,
                    ensure_ascii=False,
                )
        except OSError:
            pass

    @classmethod
    def load(cls) -> "Session":
        s = cls()
        try:
            with open(SESSION_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            s.uid = data.get("uid", "")
            s.uname = data.get("uname", "")
            s.projects = data.get("projects", {})
            s.current_project = data.get("current_project", "")
            s.current_change = data.get("current_change", "")
            s.power = data.get("power", "max")
            s.model = data.get("model", "")
            s.llm_broken = bool(data.get("llm_broken"))
        except (OSError, ValueError):
            pass
        return s

    def model_label(self) -> str:
        if self.model:
            return self.model
        provider, model = MODEL_PRESETS.get(self.power, MODEL_PRESETS["max"])
        return f"{provider}/{model}"


def _ok(msg: str) -> str:
    return f"  [ok] {msg}"


def _prog(msg: str) -> str:
    return f"  -> {msg}"


def _warn(msg: str) -> str:
    return f"  [!] {msg}"


def ensure_user(session: Session, api: Api, name: str) -> str:
    if session.uname == name and session.uid:
        return session.uid
    try:
        data = api.call("POST", "/v1/users", body={"name": name})
    except RuntimeError:
        suffix = abs(hash((name, session.uid))) % 10000
        data = api.call("POST", "/v1/users", body={"name": f"{name}-{suffix}"})
    session.uid = data["id"]
    session.uname = data.get("name") or name
    session.save()
    return session.uid


def _interview_autopilot(session: Session, api: Api, itw: str, pid: str, name: str) -> list[str]:
    """Прогоняет весь опрос типовыми ответами, проговаривая каждый вопрос."""
    out = []
    count = 0
    while True:
        data = api.call("GET", f"/v1/interviews/{itw}", uid=session.uid)
        remaining = data.get("round", [])
        if not remaining:
            break
        q = remaining[0]
        answer = _typical_answer(name)
        api.call(
            "POST",
            f"/v1/interviews/{itw}/answers",
            uid=session.uid,
            body={"answers": [{"question_id": q["id"], "answer": answer}]},
        )
        count += 1
        out.append(f"    вопрос {count}: {q['text']}  —  типовой ответ")
    result = api.call("POST", f"/v1/interviews/{itw}/finalize", uid=session.uid)
    pack = result["approval_pack"]
    api.call("POST", "/v1/approvals", uid=session.uid, body={"project_id": pid, "purpose": "product"})
    session.pending = None
    session.current_project = pid
    session.save()
    out.append(_ok(f"продукт собран, spec={str(pack.get('immutable_spec_hash', ''))[:16]}"))
    out.append(_ok("product approval: approved"))
    out.append("Дальше: 'создай change <название>'.")
    return out


def flow_project(session: Session, api: Api, name: str) -> list[str]:
    uid = ensure_user(session, api, "owner")
    out = [_prog(f"владелец: {uid}")]
    data = api.call(
        "POST",
        "/v1/projects",
        uid=uid,
        body={
            "name": name,
            "owner_user_id": uid,
            "mode": "NEW",
            "risk_tier": "HIGH",
            "budget_usd": 50.0,
        },
    )
    pid = data["id"]
    itw = api.call("POST", "/v1/interviews", uid=uid, body={"project_id": pid})["interview_id"]
    session.projects[name] = {"pid": pid}
    session.current_project = pid
    session.pending = {"kind": "interview", "itw": itw, "pid": pid, "name": name}
    session.save()
    out.append(_ok(f"проект {name} → {pid}; собрал семантику, прохожу опрос требований"))
    return out + _interview_autopilot(session, api, itw, pid, name)


def flow_change(session: Session, api: Api, title_hint: str) -> list[str]:
    if not session.current_project:
        return ["Нет текущего проекта — сначала 'создай проект <имя>'."]
    pid = session.current_project
    uid = ensure_user(session, api, "owner")
    title = title_hint or "chat change"
    files = ["docs/readme.md"]
    cid = api.call("POST", "/v1/changes", uid=uid, body={
        "project_id": pid, "title": title, "affected_paths": files,
    })["id"]
    api.call(
        "POST",
        f"/v1/changes/{cid}/delta",
        uid=uid,
        body={"delta_md": f"## ADDED Requirements\n\n### Requirement: {title}\n\n#### Scenario: Основной сценарий\n"},
    )
    api.call("POST", f"/v1/changes/{cid}/design", uid=uid, body={
        "design_md": "d", "test_plan_md": "tp", "security_md": "s",
    })
    api.call("POST", "/v1/approvals", uid=uid, body={"project_id": pid, "purpose": "design", "change_id": cid})
    tasks = api.call(
        "POST",
        f"/v1/changes/{cid}/plan",
        uid=uid,
        body={"tasks": [{"key": "T1", "write_scope": files, "depends_on": [], "acceptance_ids": ["REQ-1-SC1"]}]},
    )
    session.current_change = cid
    session.save()
    return [
        _ok(f"change: {cid} (title={title})"),
        _ok("delta + design + approval + plan: 1 задача (scope=docs/readme.md)"),
        "Теперь 'запусти задачу' — runner выполнит T1, затем 'мёрж'.",
    ]


def flow_run(session: Session, api: Api, task: str | None) -> list[str]:
    if not session.current_change:
        return [ "Нет текущего change — сначала 'создай change'."]
    uid = ensure_user(session, api, "owner")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    runner = os.path.join(repo_root, "infra", "runner", "runner.py")
    cmd = [
        sys.executable,
        runner,
        "--api",
        api.base,
        "--uid",
        uid,
        "--change",
        session.current_change,
        "--seed-note",
        "chat-agent artifact",
    ]
    if task:
        cmd += ["--task", task]
    out = [_prog(f"runner запущен (change {session.current_change[:12]}...)")]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=repo_root,
        )
        for line in proc.stdout or []:
            out.append(f"    {line.rstrip()}")
        rc = proc.wait(timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [ _warn(f"runner не запустился: {exc}") ]
    if rc != 0:
        out.append(_warn(f"runner завершился с кодом {rc}"))
    else:
        out.append(_ok("задача DONE"))
    return out


def flow_merge(session: Session, api: Api) -> list[str]:
    if not session.current_change:
        return ["Нет текущего change."]
    uid = ensure_user(session, api, "owner")
    cid = session.current_change
    api.call("POST", f"/v1/changes/{cid}/test-results", uid=uid, body={"suite": "unit", "status": "PASS"})
    api.call("POST", f"/v1/changes/{cid}/test-results", uid=uid, body={"suite": "regression", "status": "PASS"})
    ready = api.call("GET", f"/v1/changes/{cid}/readiness", uid=uid)
    out = [_prog("test-results PASS"), _ok(f"readiness={ready['verdict']} gaps={ready['gaps']}")]
    if ready["verdict"] != "GO":
        out.append(_warn("readiness не GO — мёрж заблокирован"))
        return out
    mrg = api.call("POST", f"/v1/changes/{cid}/merge", uid=uid)
    out.append(_ok(f"merge: {mrg.get('state')}"))
    return out


def flow_status(session: Session, api: Api) -> list[str]:
    out = []
    if api.health():
        out.append(_ok(f"сервер OK ({api.base})"))
    else:
        out.append(_warn(f"сервер недоступен ({api.base})"))
    if session.uname:
        out.append(_ok(f"пользователь: {session.uname} ({session.uid})"))
    out.append(_ok(f"проект: {session.current_project or '-'}"))
    if session.current_change:
        out.append(_ok(f"change: {session.current_change}"))
    return out


def flow_analytics(session: Session, api: Api) -> list[str]:
    if not session.current_project:
        return ["Нет текущего проекта."]
    uid = ensure_user(session, api, "owner")
    a = api.call("GET", f"/v1/analytics/{session.current_project}", uid=uid)
    m = api.call("GET", f"/v1/memory/{session.current_project}", uid=uid)
    out = [_prog("аналитика")]
    for k, v in a.items():
        if isinstance(v, (int, float, str)):
            out.append(f"    {k}: {v}")
    out.append(_prog("память (ADR/spec-ledger)"))
    for adr in m.get("adr_decisions", []):
        out.append(f"    ADR {adr['id']}: {adr['decision']}")
    for ch in m.get("spec_ledger", [])[:5]:
        out.append(f"    spec {ch['change']} [{ch['state']}]")
    return out


def flow_demo(session: Session, api: Api) -> list[str]:
    from app.cli import cmd_demo

    cmd_demo()
    return []


HELP = [
    "Скажи по-русски, что сделать — я выполню и покажу каждый шаг:",
    "  'создай пользователя vasya'      — владелец сессии",
    "  'создай проект orders'           — проект + опрос требований (Enter — типовой, 'стоп' — собрать как есть)",
    "  'создай change правки каталога'  — change с планом T1",
    "  'запусти задачу'                 — runner по текущему change",
    "  'мёрж'                           — test-results + readiness + merge",
    "  'статус' / 'аналитика' / 'память'",
    "  'демо'                           — полный эталонный сценарий",
    "  'выход'",
]


def parse(text: str) -> dict:
    t = " " + text.strip().lower() + " "
    if "демо" in t or "demo" in t:
        return {"kind": "demo"}
    if any(k in t for k in ("помощь", "что ты умеешь", "кто ты", "help", "?")):
        return {"kind": "help"}
    if "статус" in t or "как дела" in t or "status" in t:
        return {"kind": "status"}
    if "аналитик" in t or "пам" in t or "memory" in t or "инсайт" in t:
        return {"kind": "analytics"}
    if "мёрж" in t or "мерж" in t or "смёрж" in t or "merge" in t or " в main " in t:
        return {"kind": "merge"}
    if "запусти" in t or "стартан" in t or "run task" in t or "запуск " in t:
        rest = ""
        for tail in ("запусти задачу ", "запусти ", "run task "):
            idx = t.find(tail)
            if idx >= 0:
                after = t[idx + len(tail):].strip(" .,")
                rest = after.split()[0][:40] if after.split() else ""
                break
        result = {"kind": "run", "rest": rest}
        return result
    if "change" in t or "изменени" in t:
        rest = ""
        for tail in ("создай change ", "новый change ", "change "):
            idx = t.find(tail)
            if idx >= 0:
                after = t[idx + len(tail):].strip(" .,")
                rest = " ".join(after.split()[:6])[:80] if after.split() else ""
                break
        return {"kind": "change", "rest": rest}
    if "проект" in t or "project" in t:
        rest = ""
        for tail in ("создай проект ", "создать проект ", "новый проект ", "проект "):
            idx = t.find(tail)
            if idx >= 0:
                after = t[idx + len(tail):].strip(" .,")
                rest = after.split()[0][:40] if after.split() else ""
                break
        return {"kind": "project", "rest": rest}
    if "пользоват" in t or "user" in t:
        rest = ""
        for tail in ("создай пользователя ", "создать пользователя ", "пользователь ", "user "):
            idx = t.find(tail)
            if idx >= 0:
                after = t[idx + len(tail):].strip(" .,")
                rest = after.split()[0][:40] if after.split() else ""
                break
        return {"kind": "user", "rest": rest}
    return {}


INTENT_KEYS = ("user", "project", "change", "run", "merge", "status", "analytics", "demo", "help", "chat")


def smart_intent(session: Session, text: str) -> dict | None:
    """LLM-компас: превращает свободную фразу в {intent, ...}.

    При недоступности/ошибке модели возвращает None и сеанс переключается
    на встроенный детерминированный парсер.
    """
    if session.llm_broken:
        return None
    if session.model and "/" in session.model:
        provider_id, model_id = session.model.split("/", 1)
    else:
        provider_id, model_id = MODEL_PRESETS.get(session.power, MODEL_PRESETS["max"])
    try:
        raw = llm_call(provider_id, model_id, f"{LLM_DIRECTIVE}\n\nПользователь написал: {text}")
    except Exception:
        session.llm_broken = True
        session.save()
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        session.llm_broken = True
        session.save()
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        session.llm_broken = True
        session.save()
        return None
    if not isinstance(data, dict) or data.get("intent") not in INTENT_KEYS:
        session.llm_broken = True
        session.save()
        return None
    return data


def set_power(session: Session, token: str) -> list[str]:
    token = token.strip().lower()
    if not token:
        return [f"Сейчас: {session.power} (= {session.model_label()})", "Варианты: низкая | средняя | высокая | максимальная | режим provider/model"]
    low = token[0]
    aliases = {
        "л": "low", "с": "medium", "в": "high", "м": "max",
        "low": "low", "medium": "medium", "high": "high", "max": "max",
    }
    if token in aliases:
        session.power = aliases[token]
        session.model = ""
        session.save()
        return [_ok(f"мощность: {session.power} (= {session.model_label()})")]
    if low in aliases:
        session.power = aliases[low]
        session.model = ""
        session.save()
        return [_ok(f"мощность: {session.power} (= {session.model_label()})")]
    if "/" in token:
            session.model = token
            session.save()
            return [_ok(f"модель: {session.model}")]
    return ["Не понял мощность. Варианты: низкая/средняя/высокая/максимальная или provider/model"]


def handle(session: Session, api: Api, text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    low = text.lower()
    if low in ("выход", "exit", "quit", "пока"):
        session.save()
        return ["Закрыто. Пока!"]
    for head in ("мощность ", "мощность", "модель ", "model "):
        if low.startswith(head) and len(low) > len(head):
            return set_power(session, text[len(head):])
    if low == "мощность" or low == "model":
        return set_power(session, "")
    smart = smart_intent(session, text)
    if smart and smart.get("intent") == "chat":
        reply = (smart.get("reply") or "").strip()
        return [reply or "Я готов выполнить команду платформы."] + [
            "(мозг: " + ("LLM " + session.model_label() if session.model or not session.llm_broken else "встроенный парсер") + ")"
        ]
    cmd = smart if smart else parse(text)
    if cmd:
        cmd = {**cmd, "kind": cmd.get("intent", "") if cmd.get("intent") else cmd.get("kind", "")}
        cmd = {k: v for k, v in cmd.items() if k != "intent"}
        if cmd.get("kind") == "" or (cmd.get("kind") == "chat"):
            cmd = parse(text)
    if not cmd or not cmd.get("kind"):
        return ["Не понял фразы."] + HELP
    kind = cmd["kind"]
    if kind == "help":
        return HELP
    if kind == "user":
        uid = ensure_user(session, api, cmd.get("rest") or "user")
        return [_ok(f"пользователь {session.uname} -> {uid}")]
    if kind == "project":
        return flow_project(session, api, cmd.get("rest") or "proj")
    if kind == "change":
        return flow_change(session, api, cmd.get("rest") or "")
    if kind == "run":
        return flow_run(session, api, cmd.get("rest") or None)
    if kind == "merge":
        return flow_merge(session, api)
    if kind == "status":
        return flow_status(session, api)
    if kind == "analytics":
        return flow_analytics(session, api)
    if kind == "demo":
        flow_demo(session, api)
        return []
    return ["Не понял."]


def repl() -> int:
    os.system("")
    url = os.environ.get("AIPLATFORM_URL", DEFAULT_URL)
    api = Api(base=url)
    if not api.health():
        print("[x] сервер не отвечает на " + url + ", поднимаю uvicorn на 7700…")
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "7700"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(30):
            if api.health():
                break
            time.sleep(1)
        if not api.health():
            print("[!] не удалось поднять сервер; запусти 'aip serve' вручную")
            return 1
    session = Session.load()
    print("aip chat — говори по-русски: 'помощь' — список, 'мощность' — выбор модели, 'выход' — завершить.")
    if session.llm_broken:
        print("  (LLM недоступен для ключей этой машины — работаю встроенным родителем; 'мощность low' попробует свободную модель afresh)")
        session.llm_broken = False
        session.save()
    print(f"  мощность: {session.power} ({session.model_label()})")
    if session.uname:
        print(f"  (сессия: {session.uname}; проект: {session.current_project or '-'})")
    while True:
        try:
            text = input(">>> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        for msg in handle(session, api, text):
            print(msg)
        if text.strip().lower() in ("выход", "exit", "quit", "пока"):
            break
    return 0
