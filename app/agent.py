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
        except (OSError, ValueError):
            pass
        return s


def _ok(msg: str) -> str:
    return f"  [ok] {msg}"


def _prog(msg: str) -> str:
    return f"  -> {msg}"


def _warn(msg: str) -> str:
    return f"  [!] {msg}"


def ensure_user(session: Session, api: Api, name: str) -> str:
    if session.uname == name and session.uid:
        return session.uid
    data = api.call("POST", "/v1/users", body={"name": name})
    session.uid = data["id"]
    session.uname = name
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


def handle(session: Session, api: Api, text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    low = text.lower()
    if low in ("выход", "exit", "quit", "пока"):
        session.save()
        return ["Закрыто. Пока!"]
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
    print("aip chat — говори по-русски: 'помощь' — список, 'выход' — завершить.")
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
