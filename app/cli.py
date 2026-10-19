"""aip — CLI для AI Engineering Platform: serve / user / demo.

    python -m app.cli serve --port 7700
    python -m app.cli user alice --url http://127.0.0.1:7700
    python -m app.cli demo
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

DEFAULT_URL = os.environ.get("AIPLATFORM_URL", "http://127.0.0.1:7700")


def _base_url() -> str:
    return os.environ.get("AIPLATFORM_URL", DEFAULT_URL)


def _hdr(uid: str) -> dict:
    return {"X-User-Id": uid}


def _call(client: httpx.Client, method: str, path: str, uid: str = "", body: dict | None = None) -> dict:
    r = client.request(method, _base_url() + path, headers=_hdr(uid) if uid else {}, json=body if body is not None else None)
    if r.status_code >= 400:
        print(f"! {method} {path} -> {r.status_code}: {r.text}", file=sys.stderr)
        raise SystemExit(2)
    return r.json()


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port)


def cmd_user(args: argparse.Namespace) -> None:
    with httpx.Client() as client:
        user = _call(client, "POST", "/v1/users", body={"name": args.name})
    print(user["id"])


def cmd_demo(args: argparse.Namespace) -> None:  # noqa: C901 — линейный сценарий
    with httpx.Client() as client:
        # пользователи
        uid = _call(client, "POST", "/v1/users", body={"name": "demo-owner"})["id"]
        alice = _call(client, "POST", "/v1/users", body={"name": "demo-alice"})["id"]
        bob = _call(client, "POST", "/v1/users", body={"name": "demo-bob"})["id"]
        print(f"users: {uid} {alice} {bob}")

        # проект
        pid = _call(client, "POST", "/v1/projects", uid, {
            "name": "demo-platform",
            "owner_user_id": uid,
            "mode": "NEW",
            "risk_tier": "HIGH",
            "budget_usd": 50.0,
        })["id"]
        for who, role in ((alice, "member"), (bob, "member")):
            _call(client, "POST", f"/v1/projects/{pid}/members", uid, {"user_id": who, "role": role})
        print(f"project: {pid}")

        # интервью: отвечаем на всё, пока не кончатся gaps
        itw = _call(client, "POST", "/v1/interviews", uid, {"project_id": pid})["interview_id"]
        answered: set[str] = set()
        for _ in range(30):
            data = _call(client, "GET", f"/v1/interviews/{itw}", uid)
            fresh = [q for q in data["round"] if q["id"] not in answered]
            if not fresh:
                break
            batch = [{"question_id": q["id"], "answer": f"Значение: показатели Qin на 30%. Затрагивается: docs/demo.md. Интеграция: нет."} for q in fresh]
            _call(client, "POST", f"/v1/interviews/{itw}/answers", uid, {"answers": batch})
            answered.update(q["id"] for q in fresh)
        pack = _call(client, "POST", f"/v1/interviews/{itw}/finalize", uid)["approval_pack"]
        print(f"PRD pack: spec={pack['immutable_spec_hash'][:20]}…")

        # product approval
        _call(client, "POST", "/v1/approvals", uid, {"project_id": pid, "purpose": "product"})
        print("product approved")

        # change pack
        cid = _call(client, "POST", "/v1/changes", uid, {"project_id": pid, "title": "demo change", "affected_paths": ["docs/demo.md"]})["id"]
        _call(client, "POST", f"/v1/changes/{cid}/delta", uid, {"delta_md": "## ADDED Requirements\n\n### Requirement: Demo flow\n\n#### Scenario:все ок\n"})
        _call(client, "POST", f"/v1/changes/{cid}/design", uid, {
            "design_md": "d", "test_plan_md": "tp", "security_md": "s",
            "threat_model_md": "tm", "operations_md": "restore plan ok",
        })
        _call(client, "POST", "/v1/approvals", uid, {"project_id": pid, "purpose": "design", "change_id": cid})
        print(f"change approved: {cid}")

        # планирование DAG (2 задачи) + run + finish
        plan_result = _call(client, "POST", f"/v1/changes/{cid}/plan", uid, {"tasks": [
            {"key": "demo-1", "write_scope": ["src/demo/a.py"], "depends_on": [], "acceptance_ids": ["REQ-DEMO-SC1"]},
            {"key": "demo-2", "write_scope": ["src/demo/b.py"], "depends_on": ["demo-1"], "acceptance_ids": ["REQ-DEMO-SC1"]},
        ]})
        print(f"planned: {plan_result}")

        import hashlib
        for key in plan_result["tasks"]:
            run = _call(client, "POST", f"/v1/changes/{cid}/tasks/{key}/run", uid, {})
            sha = hashlib.sha256(key.encode()).hexdigest()[:40]
            _call(client, "POST", f"/v1/runs/{run['run_id']}/finish", uid, {"commit_sha": sha, "test_result": "PASS", "diff_summary": f"ok {key}"})
            print(f"run ok: {key} commit={sha[:12]}…")

        # finding + verifier + evidence
        fid = _call(client, "POST", f"/v1/changes/{cid}/findings", alice, {
            "classification": "BOGUS_FUNCTIONALITY", "severity": "MEDIUM",
            "file": "src/demo/a.py", "evidence_uri": "tests/test_demo_a.py",
        })["id"]
        _call(client, "POST", f"/v1/findings/{fid}/verify", bob, {"verdict": "CONFIRMED", "reproduction": "pytest::tests/test_demo_a.py"})
        _call(client, "POST", f"/v1/changes/{cid}/test-results", alice, {"suite": "unit", "status": "PASS"})
        _call(client, "POST", f"/v1/changes/{cid}/test-results", alice, {"suite": "regression", "status": "PASS"})
        ready = _call(client, "GET", f"/v1/changes/{cid}/readiness", alice)
        print(f"readiness: {ready['verdict']} gaps={ready['gaps']}")
        if ready["verdict"] != "GO":
            print("!! readiness не GO — демо останавливается", file=sys.stderr)
            raise SystemExit(3)

        # release → staging deploy → prod REFUSED → merge
        release = _call(client, "POST", f"/v1/changes/{cid}/releases", uid, {})
        rid = release["id"]
        dep = _call(client, "POST", f"/v1/releases/{rid}/deploy", uid, {"environment": "staging"})
        print(f"staging: {dep['state']}")
        try:
            _call(client, "POST", f"/v1/releases/{rid}/deploy", uid, {"environment": "production"})
        except SystemExit:
            print("production deploy: REFUSED (ожидаемо)")
        mrg = _call(client, "POST", f"/v1/changes/{cid}/merge", uid)
        print(f"merge: {mrg['state']}")

        # PSLC / оценка эффекта
        inc = _call(client, "POST", "/v1/incidents", alice, {"project_id": pid, "impact": "демо-инцидент", "traces": []})["id"]
        reg = _call(client, "POST", f"/v1/incidents/{inc}/rca", uid, {
            "rca": "демо-rca",
            "regression_case": {"scenario": "demo-repro"},
        })
        print(f"incident rca: {reg['regression_requirement']}")

        dash = _call(client, "GET", f"/v1/pslc-dashboard/{pid}", alice)
        an = _call(client, "GET", f"/v1/analytics/{pid}", alice)
        print(json.dumps({"dashboard": dash, "analytics": an}, indent=2, ensure_ascii=False))

        # eval report (EPIC-18): planted дефект найден reviewer-ом; ложный CONFIRM от не-планта
        ev = _call(client, "POST", "/v1/evals/reports", uid, {
            "project_id": pid,
            "planted_cases": {fid: "CONFIRMED"},
            "reviewer_results": {fid: "CONFIRMED", "fake-x1": "CONFIRMED"},
        })
        print(f"eval: {ev}")

        # audit chain
        chain = _call(client, "GET", f"/v1/projects/{pid}/audit", uid)
        print(f"audit: {len(chain['events'])} events, chain_ok={chain['chain_ok']}")
        print("DEMO COMPLETE")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="aip", description="AI Engineering Platform CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="запустить сервер")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=int(os.environ.get("AIPLATFORM_PORT", 7700)))
    s.add_argument("--db", default="aip.db", help="path к SQLite (для serve)")
    s.set_defaults(fn=cmd_serve)

    u = sub.add_parser("user", help="создать пользователя и напечатать id")
    u.add_argument("name")
    u.set_defaults(fn=cmd_user)

    d = sub.add_parser("demo", help="прогнать демо-поток против запущенного сервера")
    d.set_defaults(fn=cmd_demo)

    unknown_parser = parser.parse_args(argv)
    if getattr(unknown_parser, "db", None):
        os.environ["AIPLATFORM_DB_URL"] = "sqlite:///" + os.path.abspath(unknown_parser.db).replace("\\", "/")
    unknown_parser.fn(unknown_parser)


if __name__ == "__main__":
    main()
