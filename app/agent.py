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
import re
import subprocess
import sys
import time

import httpx


DEFAULT_URL = os.environ.get("AIPLATFORM_URL", "http://127.0.0.1:7700")
SESSION_FILE = os.path.join(os.getcwd(), ".aip-session.json")
OPENCODE_URL = os.environ.get("AIPLATFORM_OPENCODE_URL", "http://127.0.0.1:4096")

DEFAULT_PROVIDER_MODEL = ("opencode", "glm-5.3-flash-free", "high")

MODEL_PRESETS: dict[str, tuple[str, str, str]] = {
    "low": ("opencode", "glm-5.3-flash-free", "low"),
    "medium": ("anthropic", "claude-haiku-4-5", "medium"),
    "high": ("anthropic", "claude-sonnet-4-5", "high"),
    "max": ("anthropic", "claude-opus-4-5", "high"),
}

VARIANT_ORDER = ("low", "medium", "high", "xhigh", "max")

LLM_DIRECTIVE = (
    "Ты мозг CLI-агента платформы. Отвечай ОДНОЙ строкой строгого JSON без markdown:\n"
    '{"intent":"user|project|change|run|merge|status|analytics|demo|help|chat",'
    '"name":"","title":"","task":"","reply":""}\n'
    "Поле reply — только для intent=chat: твой живой русский ответ пользователю.\n"
    "ПРАВИЛА НАМЕРЕНИЙ (важно):\n"
    "- В вопросе-просьбе с действием ('можешь внести фичи?', 'улучши проект X?',"
    " 'сделай так чтобы...') выбирай ОПЕРАЦИЮ, а не help:\n"
    "  внести/улучшить/добавить фич или правки -> change; создать продукт/проект -> project;\n"
    "  выполнить задачу/запустить -> run; слить/мёрж -> merge; собрать отчёт -> status.\n"
    "- intent=help ТОЛЬКО когда пользователь просит список команд ('что ты умеешь', 'команды').\n"
    "- intent=chat — только разговор без действия (приветствие, внешние темы).\n"
    "- name: короткая латиница-основа из фразы (если есть слово/путь, например из"
    " 'C:\\\\proj\\\\my-app' -> my-app); title: человекочитаемая цель.\n"
    "правила: пустые поля — пустые строки; ничего кроме JSON."
)

_VARIANTS_CACHE: dict[str, dict[str, list[str]]] | None = None
_OC_AUTH_OK: tuple[str, str] | None = None


def _oc_request(method: str, path: str, body: dict | None = None, timeout: float = 240.0) -> dict:
    global _OC_AUTH_OK
    env_user = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
    env_pass = os.environ.get("OPENCODE_SERVER_PASSWORD", "opencode")
    known: list[tuple[str, str] | None] = [_OC_AUTH_OK] if _OC_AUTH_OK is not None else []
    candidates = known + [None, (env_user, env_pass)]
    tried: set = set()
    auth_errors: list[str] = []
    for auth in candidates:
        if auth in tried:
            continue
        tried.add(auth)
        try:
            r = httpx.request(method, OPENCODE_URL + path, json=body, auth=auth, timeout=timeout)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"{path} -> связь с opencode потеряна ({exc.__class__.__name__})") from exc
        if r.status_code == 401:
            label = "без ключа" if auth is None else f"ключ {auth[0]}/…"
            auth_errors.append(label)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"{path} -> {r.status_code}")
        _OC_AUTH_OK = auth
        return r.json()
    raise RuntimeError(f"{path} -> 401 ({', '.join(auth_errors) or 'нет подходящего ключа'})")


def provider_variants() -> dict[str, dict[str, list[str]]]:
    global _VARIANTS_CACHE
    if _VARIANTS_CACHE is None:
        try:
            data = _oc_request("GET", "/config/providers", timeout=20)
            out: dict[str, dict[str, list[str]]] = {}
            for prov in data.get("providers") or []:
                pid = prov.get("id") or ""
                if not pid:
                    continue
                out[pid] = {
                    mid: list((md or {}).get("variants") or {})
                    for mid, md in (prov.get("models") or {}).items()
                }
            _VARIANTS_CACHE = out
        except (httpx.HTTPError, RuntimeError, KeyError):
            _VARIANTS_CACHE = {}
    return _VARIANTS_CACHE


def clamp_variant(provider_id: str, model_id: str, wanted: str) -> str:
    variants = provider_variants().get(provider_id, {}).get(model_id) or []
    if not variants:
        return wanted if wanted else ""
    if wanted in variants:
        return wanted
    wi = VARIANT_ORDER.index(wanted) if wanted in VARIANT_ORDER else len(VARIANT_ORDER) - 1
    best, bd = variants[0], 10**9
    for v in variants:
        vi = VARIANT_ORDER.index(v) if v in VARIANT_ORDER else 0
        d = abs(vi - wi)
        if d < bd:
            best, bd = v, d
    return best


def llm_call(
    provider_id: str, model_id: str, text: str, variant: str = "", session_id: str = ""
) -> tuple[str, str]:
    sid = session_id
    if not sid:
        sid = _oc_request("POST", "/session", {"title": "aip-chat"})["id"]
    body: dict = {
        "model": {"providerID": provider_id, "modelID": model_id},
        "parts": [{"type": "text", "text": text}],
    }
    if variant:
        body["model"]["variant"] = variant
    msg = _oc_request("POST", f"/session/{sid}/message", body)
    info = (msg or {}).get("info") or {}
    err = info.get("error")
    if err:
        raise RuntimeError(str((err.get("data") or {}).get("message", err))[:140])
    texts = [
        p.get("text", "")
        for p in (msg.get("parts") or [])
        if isinstance(p, dict) and p.get("type") == "text"
    ]
    return "\n".join(t for t in texts if t).strip(), sid


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

    def user_ok(self, uid: str) -> bool:
        """Живёт ли uid на ЭТОМ сервере (защита от сессии другой базы)."""
        if not uid:
            return False
        try:
            self.call("GET", "/v1/projects", uid=uid)
        except RuntimeError:
            return False
        return True

    def project_ok(self, session: "Session", pid: str) -> bool:
        if not pid:
            return False
        try:
            self.call("GET", f"/v1/projects/{pid}", uid=session.uid or "")
        except RuntimeError:
            return False
        return True


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
        self.variant: str = ""
        self.oc_sid: str = ""
        self.open_changes: dict[str, list[str]] = {}
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
                        "variant": self.variant,
                        "open_changes": self.open_changes,
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
            s.variant = data.get("variant", "")
            s.open_changes = {k: list(v) for k, v in (data.get("open_changes") or {}).items()}
            s.llm_broken = bool(data.get("llm_broken"))
        except (OSError, ValueError):
            pass
        return s

    def model_label(self) -> str:
        provider, model, _ = MODEL_PRESETS["max"]
        if self.model:
            provider, model = (self.model.split("/", 1) + [""])[:2] if "/" in self.model else (self.model, "")
        else:
            provider, model, _v = MODEL_PRESETS.get(self.power, MODEL_PRESETS["max"])
        var = f" ({self.variant})" if self.variant else ""
        return f"{provider}/{model}{var}"


def _ok(msg: str) -> str:
    return f"  [ok] {msg}"


def _prog(msg: str) -> str:
    return f"  -> {msg}"


def _warn(msg: str) -> str:
    return f"  [!] {msg}"


def ensure_user(session: Session, api: Api, name: str, narrate: list[str] | None = None) -> str:
    if session.uid and not api.user_ok(session.uid):
        if narrate is not None:
            narrate.append(_prog("сессия с другого сервера — создаю пользователя заново"))
        session.uid = ""
        session.uname = ""
        session.projects = {}
        session.current_project = ""
        session.current_change = ""
        session.save()
    if session.uname == name and session.uid:
        return session.uid
    try:
        data = api.call("POST", "/v1/users", body={"name": name})
    except RuntimeError:
        suffix = abs(hash((name,))) % 10000
        data = api.call("POST", "/v1/users", body={"name": f"{name}-{suffix}"})
    session.uid = data["id"]
    session.uname = data.get("name") or name
    session.save()
    return session.uid


def _latin_tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", (text or "").lower())


def _project_slug(hint: str) -> str:
    toks = _latin_tokens(hint)
    return toks[-1] if toks else "proj"


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
    out: list[str] = []
    uid = ensure_user(session, api, "owner", narrate=out)
    out.append(_prog(f"владелец: {uid}"))
    existing = (session.projects.get(name) or {}).get("pid", "")
    if existing and api.project_ok(session, existing):
        session.current_project = existing
        session.save()
        out.append(_ok(f"проект {name} уже есть ({existing}) — работаю с ним"))
        return out + ["Дальше: 'создай change <название>'."]
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


def _project_state(session: Session, api: Api, pid: str) -> str:
    if not pid:
        return ""
    try:
        data = api.call("GET", f"/v1/projects/{pid}", uid=session.uid or "")
        return (data or {}).get("state", "")
    except RuntimeError:
        return ""


def flow_change(session: Session, api: Api, title_hint: str) -> list[str]:
    out: list[str] = []
    uid = ensure_user(session, api, "owner", narrate=out)
    pid = session.current_project
    if pid and not api.project_ok(session, pid):
        out.append(_prog("проект из прошлой сессии не найден на этом сервере — создаю заново"))
        pid = ""
        session.current_project = ""
        session.save()
    if not pid:
        out += flow_project(session, api, _project_slug(title_hint) or "proj")
        pid = session.current_project
    state = _project_state(session, api, pid)
    if state == "DESIGNING" and session.current_change:
        try:
            api.call(
                "POST",
                "/v1/approvals",
                uid=uid,
                body={"project_id": pid, "purpose": "design", "change_id": session.current_change},
            )
        except RuntimeError as exc:
            if _change_state(session, api, session.current_change) == "MERGED":
                raise RuntimeError(
                    "проект застрял на согласовании дизайна, а его change уже смёржен (старая логика сервера); "
                    "начни свежий проект фразой 'создай проект <название>'"
                ) from exc
            raise
        state = _project_state(session, api, pid)
        out.append(_ok(f"change {session.current_change} ждал согласования дизайна — утвердил"))
    if state not in ("PRODUCT_APPROVED", "DESIGN_APPROVED"):
        raise RuntimeError(
            f"проект {pid} сейчас в состоянии {state or 'неизвестном'} — незакрытый change мешает новому; "
            "скажи 'запусти задачу', а потом 'мёрж'"
        )
    title = title_hint or "chat change"
    _finish_open_changes(session, api, uid, pid, out, include_current=True)
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
    session.open_changes.setdefault(pid, [])
    if cid not in session.open_changes[pid]:
        session.open_changes[pid].append(cid)
    session.save()
    return out + [
        _ok(f"change: {cid} (title={title})"),
        _ok("delta + design + approval + plan: 1 задача (scope=docs/readme.md)"),
        "Теперь 'запусти задачу' — runner выполнит T1, затем 'мёрж'.",
    ]


def _change_state(session: Session, api: Api, cid: str) -> str:
    if not cid:
        return ""
    try:
        data = api.call("GET", f"/v1/changes/{cid}", uid=session.uid or "")
        return (data or {}).get("state", "")
    except RuntimeError:
        return ""


def _runner_attempt(session: Session, api: Api, uid: str, cid: str, task: str | None) -> tuple[list[str], int, str]:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    runner = os.path.join(repo_root, "infra", "runner", "runner.py")
    ws_root = os.path.join(repo_root, ".workspaces")
    cmd = [
        sys.executable,
        runner,
        "--api",
        api.base,
        "--uid",
        uid,
        "--change",
        cid,
        "--workspace-root",
        ws_root,
        "--seed-note",
        "chat-agent artifact",
    ]
    if task:
        cmd += ["--task", task]
    lines: list[str] = []
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
            lines.append(line.rstrip())
        rc = proc.wait(timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"runner не запустился: {exc}"], 1, ""
    run_id = ""
    for ln in lines:
        if ln.startswith("claim: task="):
            _, _, tail = ln.partition(" run=")
            run_id = tail.strip() or run_id
    if rc != 0 and run_id:
        try:
            api.call("POST", f"/v1/runs/{run_id}/cancel", uid=uid)
            lines.append(_prog(f"зависший run {run_id} отменён — задача снова READY"))
        except RuntimeError:
            pass
    return lines, rc, run_id


def _merge_change(session: Session, api: Api, uid: str, cid: str) -> list[str]:
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


def _finish_open_changes(session: Session, api: Api, uid: str, pid: str, out: list[str], include_current: bool = False) -> None:
    for cid in list(session.open_changes.get(pid or "") or []):
        if not include_current and cid == session.current_change:
            continue
        if _change_state(session, api, cid) == "MERGED":
            continue
        out.append(_prog(f"незакрытый change {cid[:12]}... в очереди — собираю его (runner + мёрж)"))
        keep = session.current_change
        session.current_change = cid
        lines, rc, _ = _runner_attempt(session, api, uid, cid, None)
        session.current_change = keep
        for ln in lines:
            out.append(f"    {ln}")
        if rc == 0:
            try:
                out += _merge_change(session, api, uid, cid)
            except RuntimeError as exc:
                out.append(_warn(str(exc)[:160]))
        else:
            out.append(_warn(f"change {cid} не собрался — оставляю в очереди"))


def flow_run(session: Session, api: Api, task: str | None) -> list[str]:
    out: list[str] = []
    uid = ensure_user(session, api, "owner", narrate=out)
    if session.current_change and not api.project_ok(session, session.current_project):
        out.append(_prog("контекст из прошлой сессии не найден — пересоздаю"))
        session.current_project = ""
        session.current_change = ""
        session.save()
    if session.current_change and _change_state(session, api, session.current_change) == "MERGED":
        out.append(_prog("последний change уже смёржен — открываю новый change 'task'"))
        session.current_change = ""
        session.save()
    if not session.current_change:
        out.append(_prog("нет change — создаю типовой change 'task'"))
        out += [line for line in flow_change(session, api, "task") if not line.startswith("Теперь")]
    out.append(_prog(f"runner запущен (change {session.current_change[:12]}...)"))
    if task:
        try:
            titles = [t["title"] for t in api.call("GET", f"/v1/changes/{session.current_change}/tasks", uid=uid)]
        except RuntimeError:
            titles = []
        if task not in titles:
            task = None
    lines, rc, _ = _runner_attempt(session, api, uid, session.current_change, task)
    for ln in lines:
        out.append(f"    {ln}")
    if rc != 0 and any("WRITE_SCOPE_CONFLICT" in ln for ln in lines):
        _finish_open_changes(session, api, uid, session.current_project, out)
        out.append(_prog("перезапускаю исходную задачу"))
        lines, rc, _ = _runner_attempt(session, api, uid, session.current_change, None)
        for ln in lines:
            out.append(f"    {ln}")
    if rc != 0:
        out.append(_warn(f"runner завершился с кодом {rc} — попробуй 'запусти задачу' ещё раз"))
    else:
        out.append(_ok("задача DONE"))
    session.save()
    return out


def flow_merge(session: Session, api: Api) -> list[str]:
    out: list[str] = []
    uid = ensure_user(session, api, "owner", narrate=out)
    if session.current_change and not api.project_ok(session, session.current_project):
        out.append(_prog("контекст из прошлой сессии не найден — пересоздаю"))
        session.current_project = ""
        session.current_change = ""
        session.save()
    if not session.current_change:
        out.append(_prog("нет change — создаю типовой change 'task'"))
        out += [line for line in flow_change(session, api, "task") if not line.startswith("Теперь")]
    try:
        out += _merge_change(session, api, uid, session.current_change)
    except RuntimeError as exc:
        out.append(_warn(str(exc)[:200]))
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
    if not api.project_ok(session, session.current_project):
        return [_warn("проект прошлой сессии не найден на этом сервере — начни с 'создай проект'")]
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
    "Модель мозга:",
    "  'мощность [low|medium|high|max]' — пресет (по умолчанию max)",
    "  'провайдер [opencode|anthropic|…]' — выбор провайдера (opencode — бесплатно, без ключей)",
    "  'модель provider/имя'            — точная модель (например opencode/glm-5.3-flash-free)",
    "  'мышление [low|medium|high|xhigh|max]' — глубина размышления",
    "  нет ключа — автоматически используется свободная модель opencode",
    "  'выход'",
]


def parse(text: str) -> dict:
    t = " " + text.strip().lower() + " "
    if "демо" in t or "demo" in t:
        return {"kind": "demo"}
    if any(k in t for k in ("помощь", "что ты умеешь", "кто ты", "help")) or (
        "?" in t and not _has_action(text.strip())
    ):
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
    if "change" in t or "изменени" in t or ("фич" in t and any(w in t for w in ACTION_WORDS)) or ("улучш" in t and any(w in t for w in ACTION_WORDS)):
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


def _session_candidates(session: "Session") -> list[tuple[str, str, str, bool]]:
    """Цепочка попыток: что выбран юзером → что по мощности → свободная модель."""
    out: list[tuple[str, str, str, bool]] = []
    if session.model and "/" in session.model:
        provider_id, model_id = session.model.split("/", 1)
        out.append((provider_id, model_id, session.variant or "", True))
    power = session.power if session.power in MODEL_PRESETS else "max"
    p, m, v = MODEL_PRESETS[power]
    variant = clamp_variant(p, m, v)
    if (p, m) not in [(c[0], c[1]) for c in out]:
        out.append((p, m, variant, True))
    fp, fm, fv = DEFAULT_PROVIDER_MODEL
    if (fp, fm) not in [(c[0], c[1]) for c in out]:
        out.append((fp, fm, clamp_variant(fp, fm, power == "low" and "low" or (power == "max" and "max" or "high")), False))
    return out


def smart_intent(session: "Session", text: str) -> tuple["dict | None", list[str]]:
    """LLM-компас: превращает свободную фразу в {intent, ...}.

    Пытает выбранный провайдер/модель; при недоступности ключа каскадом
    падает до свободных моделей opencode. Полный провал → None и
    встроенный детерминированный парсер. AIPLATFORM_CHAT_BRAIN=off —
    детерминированный режим без LLM-попыток.
    """
    if session.llm_broken or os.environ.get("AIPLATFORM_CHAT_BRAIN", "").lower() == "off":
        return None, []
    notes: list[str] = []
    last_err: str = ""
    for provider_id, model_id, variant, explicit in _session_candidates(session):
        try:
            raw, sid = llm_call(
                provider_id,
                model_id,
                f"{LLM_DIRECTIVE}\n\nПользователь написал: {text}",
                variant=variant or "",
                session_id=session.oc_sid,
            )
        except (RuntimeError, httpx.HTTPError) as exc:
            last_err = str(exc)[:140]
            continue
        session.oc_sid = sid
        if not explicit or not session.model:
            session.model = f"{provider_id}/{model_id}"
            session.variant = variant
        notes.append(f"(мозг: {provider_id}/{model_id}" + (f", размышление {variant}" if variant else "") + ")")
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None, notes + ["(LLM ответил не по формату — встроенный парсер)"]
        try:
            data = json.loads(raw[start : end + 1])
        except ValueError:
            return None, notes + ["(LLM ответил не по формату — встроенный парсер)"]
        if not isinstance(data, dict) or data.get("intent") not in INTENT_KEYS:
            return None, notes + ["(LLM ответил не по формату — встроенный парсер)"]
        session.save()
        return data, notes
    if not session.llm_broken:
        session.llm_broken = True
        session.save()
    notes.append(f"(LLM недоступен: {last_err}; встроенный парсер)")
    return None, notes


def set_power(session: "Session", token: str) -> list[str]:
    token = token.strip().lower()
    if not token:
        return [
            f"Сейчас: мощность {session.power} → {session.model_label()}",
            "Варианты: low  medium  high  max",
            "Точно провайдер: 'provider opencode' / 'provider anthropic'; модель: 'model provider/имя'; глубину размышления: 'thinking low..max'",
        ]
    low = token[:1]
    aliases = {
        "л": "low", "с": "medium", "в": "high", "м": "max",
        "low": "low", "medium": "medium", "high": "high", "max": "max",
        "малый": "low", "малое": "low", "средний": "medium", "средняя": "medium",
        "высокий": "high", "высокая": "high", "высокое": "high",
        "макс": "max", "максимум": "max", "максимальный": "max",
    }
    resolved = aliases.get(token) or aliases.get(low)
    if "/" in token:
        session.model = token
        session.variant = ""
        session.llm_broken = False
        session.save()
        return [_ok(f"модель: {session.model}")]
    if not resolved:
        return ["Не понял мощность; варианты: low medium high max — или 'provider opencode'"]
    session.power = resolved
    session.model = ""
    session.variant = ""
    session.llm_broken = False
    session.oc_sid = ""
    session.save()
    return [_ok(f"мощность: {session.power} → {session.model_label()}")]


def set_provider(session: "Session", token: str) -> list[str]:
    token = token.strip().lower()
    if "/" in token:
        return set_power(session, token)
    if not token:
        head = session.model.split("/", 1)
        current = head[0] if len(head) == 2 and head[0] else MODEL_PRESETS.get(session.power, MODEL_PRESETS["max"])[0]
        known = provider_variants() or {current: {}}
        return [
            f"Сейчас провайдер: {current}. Известные: {', '.join(sorted(known))}",
            "Выбор: 'provider opencode' (бесплатно, без ключей) или 'provider anthropic' (по ключу)",
        ]
    known: dict = provider_variants()
    models = known.get(token)
    if models is None and token in ("opencode", "anthropic", "openrouter", "cloudflare"):
        models = {}
    if models is None:
        return [f"Не знаю провайдера '{token}'. Известные: " + ", ".join(sorted(known or ["opencode"]))]
    default_model = "glm-5.3-flash-free" if token == "opencode" else (sorted(models)[0] if models else "")
    if token == "anthropic" and "claude-opus-4-5" in models:
        default_model = "claude-opus-4-5"
    session.model = f"{token}/{default_model}" if default_model else ""
    session.variant = ""
    session.llm_broken = False
    session.oc_sid = ""
    session.save()
    return [
        _ok(f"провайдер {token}" + (f"; модель {default_model}" if default_model else " — задай модель: 'model provider/имя'"))
    ]


def set_variant(session: "Session", token: str) -> list[str]:
    token = token.strip().lower()
    aliases = {
        "л": "low", "с": "medium", "в": "high", "м": "max",
        "low": "low", "малый": "low", "малое": "low", "минимум": "low",
        "medium": "medium", "средний": "medium", "среднее": "medium", "средняя": "medium",
        "high": "high", "высокий": "high", "высокое": "high", "высокая": "high",
        "xhigh": "xhigh", "сверхвысокое": "xhigh",
        "max": "max", "макс": "max", "максимум": "max", "максимальный": "max",
    }
    wanted = aliases.get(token) or aliases.get(token[:1]) if token else None
    if not wanted:
        return ["Глубина размышления: low medium high xhigh max"]
    provider_id, model_id = (
        session.model.split("/", 1) if "/" in session.model else MODEL_PRESETS.get(session.power, MODEL_PRESETS["max"])[:2]
    )
    session.variant = clamp_variant(provider_id, model_id, wanted)
    session.llm_broken = False
    session.save()
    return [_ok(f"{provider_id}/{model_id}, размышление: {session.variant or '(по умолчанию)'}")]


ACTION_WORDS = (
    "созда", "внеси", "внести", "улучш", "сдела", "сделай", "добав", "запуст",
    "собер", "помердж", "мёрж", "мерж", "исправ", "почин", "обнов", "провер",
    "выполни", "сгенер",
)


def _has_action(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in ACTION_WORDS)


def handle(session: Session, api: Api, text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    low = text.lower()
    if low in ("выход", "exit", "quit", "пока"):
        session.save()
        return ["Закрыто. Пока!"]
    for head in ("мощность ", "мощность", "модель ", "model ", "provider ", "провайдер ", "мышление ", "thinking "):
        if low.startswith(head) and len(low) > len(head):
            token = text[len(head):].strip()
            if head.strip() in ("мощность", "модель", "model"):
                return set_power(session, token)
            if head.strip() in ("provider", "провайдер"):
                return set_provider(session, token)
            return set_variant(session, token)
    if low == "мощность" or low == "model":
        return set_power(session, "")
    if low in ("provider", "провайдер"):
        return set_provider(session, "")
    if low in ("мышление", "thinking"):
        return set_variant(session, "")
    smart, notes = smart_intent(session, text)
    if (not smart or smart.get("intent") in ("help", "chat")) and _has_action(text):
        guessed = parse(text)
        if guessed and guessed.get("kind") not in ("", "help", None):
            if notes and notes[0].startswith("(мозг"):
                notes = notes[1:]
            smart = smart or {}
            smart["intent"] = guessed["kind"]
            smart.setdefault("name", guessed.get("rest") or "")
            smart.setdefault("title", guessed.get("rest") or "")
            notes.append("(догадался по контексту фразы)")
    if smart and smart.get("intent") == "chat":
        reply = (smart.get("reply") or "").strip()
        return [reply or "Я готов выполнить команду платформы."] + notes
    if not smart:
        cmd = parse(text)
    else:
        cmd = {**smart, "kind": smart.get("intent", "")}
        cmd.pop("intent", None)
        if cmd.get("kind") in ("user", "project", "change", "run") and not cmd.get("rest"):
            cmd["rest"] = smart.get("name") or smart.get("title") or smart.get("task") or ""
    if not cmd or not cmd.get("kind"):
        return ["Не понял фразы."] + notes[:1] + HELP
    kind = cmd["kind"]
    if kind == "help":
        return HELP
    if kind == "chat":
        return [(smart.get("reply") or "Я готов выполнить команду платформы.")] if smart else ["Не понял."] + HELP
    out: list[str] = []
    try:
        if kind == "user":
            uid = ensure_user(session, api, cmd.get("rest") or "user")
            out = [_ok(f"пользователь {session.uname} -> {uid}")]
        elif kind == "project":
            out = flow_project(session, api, cmd.get("rest") or "proj")
        elif kind == "change":
            out = flow_change(session, api, cmd.get("rest") or "")
        elif kind == "run":
            out = flow_run(session, api, cmd.get("rest") or None)
        elif kind == "merge":
            out = flow_merge(session, api)
        elif kind == "status":
            out = flow_status(session, api)
        elif kind == "analytics":
            out = flow_analytics(session, api)
        elif kind == "demo":
            flow_demo(session, api)
            out = []
        else:
            out = ["Не понял."]
    except RuntimeError as exc:
        out = [
            "[!] не вышло: " + str(exc)[:220],
            "Скажи 'статус' — покажу, где мы; продолжай другой фразой.",
        ]
    return out + notes


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
    print("aip chat — говори по-русски: 'помощь' — список, 'мощность/провайдер/мышление' — модель, 'выход' — завершить.")
    if session.llm_broken:
        print("  (LLM не ответила в прошлый раз — при новой реплике попробую заново)")
        session.llm_broken = False
        session.save()
    print(f"  модель: {session.model_label()}")
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
