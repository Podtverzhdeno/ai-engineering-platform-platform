# Runbook: недоступность LLM-провайдера (R-05)

Симптом: 403/429/timeout провайдера, задача агентa не стартует.
Действия:
1. Проверить флаг `AIPLATFORM_OPENCODE_LIVE` и доступ к провайдеру.
2. Sistema переводит задачу в WAITING_MODEL (конфигурация по умолчанию) — workflow не разрушается.
3. Переключить на fallback-модель в проектной policy (`project-level provider allowlist`, §10.1 файла ТЗ; R-04).
4. При остановленной инфраструктуре: остановить деплой, голосование по release-compose 继续 через Control API (не останавливать Control Plane).
