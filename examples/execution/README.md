# Пример управляемого запуска

Пример предназначен для установленного GIGACODE CLI с поддержкой флагов managed-адаптера. Он анализирует только синтетическое требование; продукт и внешние системы не проверяются. В текущей среде GIGACODE отсутствует, поэтому живой прогон этого примера не заявлен. Механика supervisor проверена отдельными процессными тестами.

Из корня проекта, для **новых** task/execution/result путей:

```bash
python3 scripts/manage_task.py --state .qa-agent-state/tasks/QA-MANAGED/task.json init --task-id QA-MANAGED --title "Синтетический анализ" --criteria examples/execution/criteria.json
python3 scripts/manage_execution.py --state .qa-agent-state/tasks/QA-MANAGED/execution.json init --plan examples/execution/plan.json
python3 scripts/manage_execution.py --state .qa-agent-state/tasks/QA-MANAGED/execution.json serve --max-seconds 60
python3 scripts/manage_execution.py --state .qa-agent-state/tasks/QA-MANAGED/execution.json show
```

Если GIGACODE недоступен или не поддерживает обязательные флаги, выполнение блокируется с configuration, без вымышленного результата. При валидном результате execution переходит в awaiting_review. Прочитайте фактический отчёт/артефакты, затем получите текущую ревизию task через show и передайте её в review:

```bash
python3 scripts/manage_task.py --state .qa-agent-state/tasks/QA-MANAGED/task.json show
python3 scripts/manage_task.py --state .qa-agent-state/tasks/QA-MANAGED/task.json review --request-id QA-MANAGED-analysis-1 --decision "Содержание и источники синтетического отчёта проверены" --expected-revision CURRENT_REVISION
python3 scripts/manage_execution.py --state .qa-agent-state/tasks/QA-MANAGED/execution.json serve --max-seconds 2
```

CURRENT_REVISION — число из фактического реестра, не готовое значение. После accepted supervisor сохраняет checkpoint. Completed у execution означает завершение этого плана; task завершается отдельно после оценки C-ANALYSIS через assess и содержательного finish по TASK_ACCEPTANCE.md. При отклонённом содержании не выдавайте принятие ради демонстрации.

Для ожидания формы заполните wait_for_questionnaire_ids заранее зарегистрированными пакетами этой задачи и запустите questionnaire.py с тем же task_state. По умолчанию перед запуском шага требуется apply-answers координатора. Для нового демонстрационного прогона используйте новые ID и пути, не удаляйте результаты прежней попытки.

`gateway-policy.json` не подключён к плану анализа: это шаблон для задач внешних действий. Замените server_command, имена, аргументы, constraints и readback по реальной схеме доверенного сервера; добавьте полученную политику в gateway_policy нужного шага. Пример не предоставляет доступ к Jira или иной системе.

Полные правила: [MANAGED_EXECUTION.md](../../docs/MANAGED_EXECUTION.md), [ACTION_GATEWAY.md](../../docs/ACTION_GATEWAY.md).
