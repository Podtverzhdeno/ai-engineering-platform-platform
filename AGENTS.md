# AGENTS.md — маршрутизатор для агентов

Внешний вид: каждый раздел — указатель правил, а не свалка документации (§6.3).

## Что здесь есть

- `app/` — Control Plane (FastAPI). Агенты НЕ имеют права писать сюда без Design Gate платформы.
- `tests/` — acceptance-тесты (EPIC-acceptance из ТЗ §16/G.2).
- `infra/` — sandbox-раннеры и compose.

## Правила для любых coding-агентов этого репозитория

1. Запрещено менять: `app/policy.py`, `app/audit.py`, `app/gates.py`, `tests/` без отдельного approval (protected paths, risk floor HIGH).
2. Запрещено читать `.env`, секреты, `*.key`, `*.pem` — runner обязан работать без production-секретов.
3. Запрещены команды shell вне worktree и сетевые запросы — транспорт OpenCode-сессий контролирует Agent Gateway.
4. Само-подтверждение собственного кода запрещено (CORE-05): верификатор ≠ автор.
5. Каждый коммит обязан ссылаться на `spec_hash` изменённой спецификации, иначе Evidence Pack неполный.

## Карта домена (§14.1)

Project / InterviewSession / Requirement / ChangeSet / ADR / Task / AgentRun / Finding /
Approval / Evidence / Release / Incident — модели в `app/models.py`, API в `app/api/`.
