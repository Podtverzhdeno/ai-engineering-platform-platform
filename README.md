# AI Platform — Control Plane

Реализация ТЗ «AI Engineering Platform 2026» (маршрут M1 → M6, Appendix G.1). Полное ТЗ: `../AI_Engineering_Platform...docx` (извлечён в `third-party`).

**Принципы (§2.1)**: Control Plane ≠ выполнение; спецификация ≠ реализация; approval привязан
к `spec_hash`/`commit_sha`; LLM-мнение ≠ доказательство; policy floor не опускается ниже решётки правил.

**Стек**: Python/FastAPI, SQLAlchemy, durable job engine (ADR-003), локальный профиль (§7.4).
OpenCode — agent runtime (S02–S07 docs сохранены), OpenSpec — канонический SDD (S08–S14), Spec Kit — методология.

## Trusted runner

```powershell
python infra\runner\runner.py --api http://127.0.0.1:7700 --uid <user-id> --change CHG-... [--task T1]
    [--seed-note "simulated output"]      # dummy-артефакт, чтобы пройти finish без реального агента
    [--execute-opencode]                  # запустить `opencode run` в workspace задачи
```

Docker-isolated runner (см. `infra/runner/Dockerfile` — node:22-slim + python3 + git + opencode,
zero-dep собственный клиент на urllib):

```powershell
docker build -t aip-runner:latest infra\runner
docker run --rm --add-host=host.docker.internal:host-gateway `
  -v <repo>\.workspaces:/workspaces -w /workspaces `
  --entrypoint python3 aip-runner:latest /app/runner.py `
  --api http://host.docker.internal:7700 --uid <uid> --change CHG-... `
  --seed-note "docker artifact" --workspace-root /workspaces
```

Runner берёт первого READY-таск change-а, создаёт run (сервер materialize-ит workspace c
`task-contract.json`), читает контракт, исполняет работу и завершает run контракт-дигестом.
Dry-run без файлов finish прерывает (нет артефактов — нет DONE). В контейнере [--workspace-root /workspaces]
те же шаги, sample live: claim → contract → seed → finish DONE (b77ec8264f…).

## Live mode (real OpenCode sessions)

`POST /run` дополнительно выполняет **реальный заход в OpenCode Server API**, если заданы:

```powershell
$env:AIPLATFORM_OPENCODE_LIVE     = "1"
$env:AIPLATFORM_OPENCODE_URL      = "http://127.0.0.1:4096"   # `opencode serve --port 4096`
$env:AIPLATFORM_OPENCODE_TIMEOUT  = "240"
# учётные данные наследуются из OPENCODE_SERVER_USERNAME / OPENCODE_SERVER_PASSWORD
.\scripts\demo_live.ps1           # поднимает опкод+сервер и прогоняет весь demo-поток live
```

Приоритеты env: `AIPLATFORM_OPENCODE_USER/PASSWORD` → `OPENCODE_SERVER_*`. Каждая задача создаёт
настоящую сессию (`ses_…`, видна в списке `/session`), в `run.traces` попадают события
`opencode.session_open` и `opencode.reply`; недоступность сервера → статус `MODEL_UNAVAILABLE`,
не 500. Смок-тест: `AIPLATFORM_OPENCODE_LIVE=1 pytest tests/test_live.py`.

## Запуск

```powershell
cd platform
.\scripts\run_dev.ps1            # venv + deps + uvicorn на 7700
aip serve                        # или .venv\Scripts\aip.exe serve — сервер на 7700
aip demo                         # полный M1→M6 сценарий против запущенного сервера
aip user alice                   # создать пользователя → напечатать id
.venv\Scripts\pytest -q          # 40 acceptance-тестов
```

CLI (`pip install -e .` даёт команду `aip`; альтернатива `python -m app.cli`).
Переменные: `AIPLATFORM_URL` (client, default 7700), `AIPLATFORM_PORT` / `--port`, `AIPLATFORM_DB_URL`.

## Chat CLI (aip chat) — свободный диалог

```powershell
aip chat                          # клиент с LLM-мозгом; сервер поднимается сам
```

UX-контракт (спека): пользователь пишет что угодно по-русски — система сама
достраивает контекст и выполняет. Из реплики «внеси фичи для улучшений каталога»
агент сам заводит пользователя → проект (автоопрос типовыми ответами) → change
с планом; «запусти задачу» / «мёрж» продолжают цепочку. Контекст (uid/проект/change)
персистентен; если сессия была против другого сервера (другая база) — uid
пересоздаётся автоматически, без 401 (heal).

Выбор мозга (LLM через opencode-сервер):

| Реплика | Действие |
|---|---|
| `мощность low\|medium\|high\|max` | пресет (default max) |
| `провайдер opencode\|anthropic\|…` | провайдер; opencode — бесплатно без ключей |
| `модель provider/имя` | точная модель |
| `мышление low..max` | глубина размышления (вариант модели) |

Каскад: если у выбранного провайдера нет рабочего ключа — автоматически
используется свободная модель `opencode/glm-5.3-flash-free` (работает без
каких-либо ключей). Каждая LLM-реплика подписана `(мозг: …)`.
`AIPLATFORM_CHAT_BRAIN=off` — детерминированный разбор без LLM (CI/тесты).

## Docker (postgres + app + runner)

```powershell
docker compose -f infra\docker-compose.yml up -d --build
#   postgres (внутренний, healthcheck pg_isready)
#   app      (http://localhost:7701 — API в контейнере, .workspaces через bind-mount)
docker build -t aip-runner:latest infra\runner
docker run --rm --network infra_default -v <repo>\.workspaces:/workspaces -w /workspaces `
  --entrypoint python3 aip-runner:latest /app/runner.py `
  --api http://app:7700 --uid <uid> --change CHG-... --seed-note "..." --workspace-root /workspaces
```

Проверено end-to-end: interview → product approval → change → design → plan → run из контейнера
runner-а во внутреннюю сеть compose (`http://app:7700`) → finish DONE. На реальном Postgres
дополнительно ловились и пофикшены два бага, невидимых на SQLite: flush-порядок FK (projects+memberships) и
кросс-проектное пересечение write scope (теперь scoped по project).

## Полный happy-path (M2/M3)

```http
POST /v1/users                                       {"name": "alice" }
POST /v1/projects  (X-User-Id)                       {"name": "shop", "mode": "NEW"}
POST /v1/interviews                                  {"project_id": "..."}     → раунд вопросов
POST /v1/interviews/{id}/answers                     [{"question_id": "Q-PROD-001", "answer": "..."}]
POST /v1/interviews/{id}/finalize                    → PRD Approval Pack + sha256
POST /v1/approvals   {purpose: "product"}            → project_state=PRODUCT_APPROVED
POST /v1/changes                                     {"title": "audit", "affected_paths": [...] }
POST /v1/changes/{id}/delta                          OpenSpec ADDED/MODIFIED/REMOVED md → spec_hash
POST /v1/changes/{id}/design                         design + threat_model + test_plan
POST /v1/approvals   {purpose: "design", change_id}  → DESIGN_APPROVED (HIGH риск требует threat-model + 2 approvals)
POST /v1/changes/{id}/plan                           task DAG; конфликт write-scope → 422
POST /v1/changes/{id}/tasks/{key}/run                sandbox-сессия; отказ при BUDGET_EXCEEDED
POST /v1/runs/{id}/finish                            structured результат (commit_sha 40 hex обязателен)
POST /v1/changes/{id}/findings + /verify             Finding Contract; verifier требует evidence
POST /v1/changes/{id}/test-results                   Evidence о unit/integration прогонах
POST /v1/changes/{id}/releases                       readiness-гейт: NO_GO при blocking findings
POST /v1/releases/{id}/deploy                        staging ок; production — REFUSED (§12.1)
POST /v1/releases/{id}/rollback
POST /v1/incidents + /rca                            регресс-тест фиксирован exercitation→ spec
```

Эндпоинты: `/docs` (OpenAPI), audit-chain по каждому проекту: `GET /v1/projects/{id}/audit`.

## Статус эпиков

| Эпик | Состояние | Proof |
|---|---|---|
| EPIC-01 foundation | ✅ | test_epic01 (RBAC, isolate, audit hash-chain, tamper detect); compose-деплой (postgres+app+runner) проверен end-to-end, два postgres-only бага пойманы и починены |
| EPIC-03 Discovery | ✅ | test_discovery (rounds, provenance, gaps, product approval) |
| EPIC-05 OpenSpec Registry | ✅ | test_changes (delta parse, hash, invalidation) |
| EPIC-06 Tech Advisor | ✅ | test_changes (matrix, ADR, ACC-01/ACC-17) |
| EPIC-07 Design Gate | ✅ | test_changes (threat-model required at HIGH risk) |
| EPIC-08/11 Agent Gateway | ✅ каркас + live | test_gates (CORE-01, E.2); **live-сессии opencode**: `AIPLATFORM_OPENCODE_LIVE=1` + `scripts/demo_live.ps1` (проверено: реальные `ses_*` сессии, traces с reply) |
| EPIC-09 Trusted Runner | ✅ | `infra/runner/runner.py` (zero-dep urllib): claim (POST run) → fetch contract (`GET /v1/runs/{id}`) → execute (`--execute-opencode` / `--seed-note` dummy) → finish (commit_sha = 40-hex артефакт-дигест); `POST /v1/runs/{id}/cancel` разлочивает RUNNING. Docker-isolated: `aip-runner` (node:22-slim+python3+git+opencode; проверен полный цикл из контейнера, `--workspace-root`) |
| EPIC-10 Planner DAG | ✅ | test_m4_m6 (scope conflict ACC-06, ready queue ACC-18, budget ACC-19) |
| EPIC-12/13 QA + Review | ✅ | test_m4_m6 (Findings §11.4, verifier ACC-08, convergence R-11) |
| EPIC-14/16 Quality/Release | ✅ | test_m4_m6 (readiness GO/NO_GO ACC-09, staging/rollback) |
| EPIC-17 PSLC | ✅ | incident → RCA → regression REQ ACC-13, dashboard, policy denials |
| EPIC-18 Eval Lab | ⏳ | план: docs/benchmark-plan.md |
| EPIC-19 Cost | ✅ мин | budget gate (BUDGET_EXCEEDED) |
| UI Workspace (§7.1) | ✅ | `app/static/index.html` — полный wizard: интервью → approvals → change/DAG → runs/findings → release/rollback → PSLC → audit chain; GET `/` |
| EPIC-15 Merge queue | ✅ | `POST /v1/changes/{id}/merge` — readiness GO обязателен; conflicts через очередь |
| EPIC-18 Eval Lab | ✅ мин | `POST /v1/evals/reports` — planted-recall + reviewer-precision, dataset-versioning (sha); `POST /v1/evals/live` — отчёт по живым аудитам (planted = `classification: planted`, рекол и точность из реальных verifier-вердиктов) |
| CI | ✅ | `.github/workflows/ci.yml` — pytest + `aip serve` smoke + docker-сборки app/runner и compose sanity |
| EPIC-20 Product Analytics | ✅ мин | `GET /v1/analytics/{pid}` — tasks delivered, review cycles, findings by verdict, budget |
| EPIC-04 Memory | ✅ мин | `app/services/memory.py` + `GET /v1/memory/{pid}` / `/prompt`: ADR-решения, spec-ledger, инцидент-уроки, правила; prompt-блок вшивается в live-dispatch агента |
| EPIC-02 Platform SDD | ✅ | сам репо соблюдает свой процесс: AGENTS.md-маршрутизатор, protected paths, acceptance-тесты как спека |

## Runbook'ы

`docs/runbooks/{provider-outage,runner-crash,data-leak-suspicion}.md`; `docs/NOTICE.md` — лицензии; `docs/benchmark-plan.md` — испытания.

