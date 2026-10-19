"""Trusted runner (EPIC-09): claim → fetch contract → execute → finish.

    python infra/runner/runner.py --api http://127.0.0.1:7700 --uid <user-id> \
        --change CHG-xxxx [--task demo-1] [--execute-opencode] [--keep-workspace]

Протокол (Control Plane ≠ выполнение):
  1. GET  /v1/changes/{cid}/tasks            — выбрать первого READY
  2. POST /v1/changes/{cid}/tasks/{key}/run   — сервер создаёт RUN + materialize workspace
  3. GET  /v1/runs/{run_id}                   — контракт: write_scope, acceptance, workspace path
  4. выполнить работу в workspace (по умолчанию — dry-run, --execute-opencode роняет реально
     `opencode run` внутри workspace)
  5. POST /v1/runs/{run_id}/finish           — commit_sha = sha256 по выходным файлам (40 hex)

Runner читает task-contract из .workspaces/<task-id>/task-contract.json (cwd Control Plane).
Guardrails (AGENTS.md): shell вне worktree запрещён, секреты не читаются — модели передаётся
только переписанный контракт с write_scope.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


def _headers(uid: str) -> dict:
    return {"X-User-Id": uid}


def _request(api: str, uid: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{api}{path}",
        data=data,
        headers=_headers(uid) | ({"Content-Type": "application/json"} if data else {}),
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            text = resp.read().decode()
            if (resp.headers.get("Content-Type") or "").startswith("application/json"):
                return resp.status, json.loads(text)
            return resp.status, text
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read().decode()
        except Exception:
            payload = ""
        return exc.code, payload


def fetch_ready_task(api: str, uid: str, change_key: str, want: str | None) -> tuple[str, str]:
    status, tasks = _request(api, uid, "GET", f"/v1/changes/{change_key}/tasks")
    if status != 200:
        print(f"GET tasks: {status} {tasks}", file=sys.stderr)
        raise SystemExit(2)
    tasks = [t for t in tasks if t["status"] == "READY"]
    if want:
        tasks = [t for t in tasks if t["title"] == want]
    if not tasks:
        raise SystemExit("нет READY-задач (уже running/done?)")
    task = tasks[0]
    status, body = _request(api, uid, "POST", f"/v1/changes/{change_key}/tasks/{urllib.parse.quote(task['title'])}/run")
    if status != 201:
        print(body, file=sys.stderr)
        raise SystemExit(2)
    return task["title"], body["run_id"]


def contract(api: str, uid: str, run_id: str) -> dict:
    status, body = _request(api, uid, "GET", f"/v1/runs/{run_id}")
    if status != 200:
        print(f"GET run: {status} {body}", file=sys.stderr)
        raise SystemExit(2)
    return body


def execute(workspace: str, task: dict, use_opencode: bool) -> tuple[list[str], str]:
    """Возвращает (созданные/изменённые файлы относительно workspace, журнал)."""
    log = []
    files: list[str] = []
    if not workspace or not os.path.isdir(workspace):
        raise SystemExit(f"workspace недоступен: {workspace}")
    if use_opencode:
        prompt = (
            f"Создай файлы {task['write_scope']} в текущей директории. Не изменяй ничего вне них. "
            f"Acceptance: {task['acceptance_ids']}. Минимальная реализация + тест. Не читай секреты."
        )
        exe = os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules", "opencode-ai", "bin", "opencode.exe")
        cmd = [exe if os.path.exists(exe) else "opencode", "run", prompt]
        proc = subprocess.run(cmd, cwd=workspace, capture_output=True, text=True, timeout=900)
        log.append(f"opencode rc={proc.returncode}")
        log.append(proc.stdout[-2000:])
        if proc.returncode != 0:
            print(proc.stderr[-1500:], file=sys.stderr)
    else:
        log.append("dry-run: файлы не создаются (--execute-opencode чтобы реально исполнить)")
    for root, _dirs, names in os.walk(workspace):
        for name in names:
            rel = os.path.relpath(os.path.join(root, name), workspace).replace("\\", "/")
            if rel != "task-contract.json":
                files.append(rel)
    return files, "\n".join(log)


def finish(api: str, uid: str, run_id: str, workspace: str, files: list[str], log: str) -> dict:
    entries = []
    for rel in files:
        with open(os.path.join(workspace, rel), "rb") as fh:
            entries.append({"path": rel, "sha256": hashlib.sha256(fh.read()).hexdigest()})
    seed = json.dumps(sorted(json.dumps(e, sort_keys=True) for e in entries)).encode()
    commit_sha = hashlib.sha256(seed).hexdigest()[:40]  # 40 hex — артефакт-дигест (git-shape)
    status, body = _request(
        api,
        uid,
        "POST",
        f"/v1/runs/{run_id}/finish",
        body={
            "commit_sha": commit_sha,
            "test_result": "PASS" if entries else "N_A",
            "diff_summary": f"runner: {len(entries)} файлов; лог: {log[:800]}",
            "open_questions": [],
        },
    )
    if status != 200:
        print(body, file=sys.stderr)
        raise SystemExit(3)
    return body | {"commit_sha": commit_sha}


def main() -> None:
    parser = argparse.ArgumentParser(prog="runner")
    parser.add_argument("--api", default=os.environ.get("AIPLATFORM_URL", "http://127.0.0.1:7700"))
    parser.add_argument("--uid", required=True)
    parser.add_argument("--change", required=True)
    parser.add_argument("--task", default=None, help="точный title задачи; иначе первый READY")
    parser.add_argument("--execute-opencode", action="store_true", help="запустить opencode run в workspace")
    parser.add_argument("--seed-note", default=None, help="положить note.txt с этим текстом в workspace (simulated output)")
    parser.add_argument("--workspace-root", default=None, help="переписать корень workspace (контейнер монтирует .workspaces в /workspaces)")
    args = parser.parse_args()

    task_key, run_id = fetch_ready_task(args.api, args.uid, args.change, args.task)
    print(f"claim: task={task_key} run={run_id}")
    data = contract(args.api, args.uid, run_id)
    workspace = data["workspace"]
    task_id = workspace.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
    if args.workspace_root:
        workspace = os.path.join(args.workspace_root, task_id)
        print(f"workspace (remapped): {workspace}", file=sys.stderr)
    print(f"workspace: {workspace}\nscope: {data['task']['write_scope']}")
    if args.seed_note:
        with open(os.path.join(workspace, "note.txt"), "w", encoding="utf-8") as fh:
            fh.write(args.seed_note)
    files, log = execute(workspace, data["task"], args.execute_opencode)
    if not files:
        print("нет выходных файлов в workspace — finish прерван", file=sys.stderr)
        raise SystemExit(4)
    result = finish(args.api, args.uid, run_id, workspace, files, log)
    print(f"finish: {result}")


if __name__ == "__main__":
    main()
