# Runbook: падение runner / контейнера OpenCode

Симптом: job RUNNING с истёкшим lease (recover_expired), agent run FAILED.
Действия:
1. `workflow.recover_expired()` переводит job обратно в RUNNABLE (job engine records idempotency: внешние эффекты не дублируются).
2. Свёрить фактическое состояние PR/CI перед повторным запросом (§7.3): reconcile по commit/CI status.
3. По свежим логам контейнера удостовериться, что secret-утечек нет (`gitleaks` scan артефакта), после чего перезапускать runner.
