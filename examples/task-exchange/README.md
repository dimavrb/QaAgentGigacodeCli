# Учебная передача через реестр

Пример проверяет только локальный протокол: регистрация → резервирование передачи → структурная проверка → записанное принятие. GIGACODE, стенд и внешние системы не запускаются. Все результаты синтетические. Команды выполняются из корня проекта; пример рассчитан на новый реестр и отдельный каталог артефактов.

```bash
python3 scripts/manage_task.py --state .qa-agent-state/tasks/QA-EXCHANGE/task.json init --task-id QA-EXCHANGE --title "Учебная передача" --criteria examples/task-exchange/criteria.json
python3 scripts/manage_task.py --state .qa-agent-state/tasks/QA-EXCHANGE/task.json assign --input examples/task-exchange/assignment.json --expected-revision 1
mkdir -p artifacts/exchange/demo
python3 scripts/manage_task.py --state .qa-agent-state/tasks/QA-EXCHANGE/task.json prepare --request-id QA-EXCHANGE-env-1 --expected-revision 2 > artifacts/exchange/demo/handoff.json
cp -n examples/task-exchange/result.json artifacts/exchange/demo/env-result.json
python3 scripts/manage_task.py --state .qa-agent-state/tasks/QA-EXCHANGE/task.json submit --request-id QA-EXCHANGE-env-1 --expected-revision 3
python3 scripts/manage_task.py --state .qa-agent-state/tasks/QA-EXCHANGE/task.json wait --after 2 --timeout 0
python3 scripts/manage_task.py --state .qa-agent-state/tasks/QA-EXCHANGE/task.json review --request-id QA-EXCHANGE-env-1 --decision "Учебный отчёт и доказательство прочитаны; принимается только синтетический протокол" --expected-revision 4
python3 scripts/manage_task.py --state .qa-agent-state/tasks/QA-EXCHANGE/task.json show
python3 scripts/manage_task.py --state .qa-agent-state/tasks/QA-EXCHANGE/task.json assess --input examples/task-exchange/assessments.json --expected-revision 5
python3 scripts/manage_task.py --state .qa-agent-state/tasks/QA-EXCHANGE/task.json finish --outcome passed --decision "Синтетический протокол проверен по C-PROTOCOL" --expected-revision 6
python3 scripts/export_task_trace.py --state .qa-agent-state/tasks/QA-EXCHANGE/task.json --output artifacts/exchange/demo/trace.jsonl
```

После review ожидается ревизия 5: назначение accepted. После assess и finish — ревизия 7, completed и принятый C-PROTOCOL. Это успех синтетического протокола без проверки продукта. handoff.json содержит критерий, trace_context и delivery_id. В реальной задаче координатор передаёт его исполнителю и записывает владельца через start; здесь исполнитель не запускался.

Следующее назначение с `depends_on: ["QA-EXCHANGE-env-1"]` допускается только после принятия. Если изменить evidence.txt или requirements.md, подготовка зависимой передачи завершится ошибкой. Не меняй учебные файлы для демонстрации в рабочей копии: используй временную копию; такие случаи уже покрыты тестами.

Для повторного прохождения создай отдельный реестр/ID и каталог результатов. Повторное назначение с тем же request_id запрещено; повторный submit неизменённого результата не создаёт дубликат события. `ack` подтверждает обработку конкретного event_id, но не принимает результат.

Результат включает self_review по docs/SELF_REVIEW.md. Это итог самопроверки синтетического примера, не доказательство продуктовых действий. Если удалить self_review, submit не публикует result_ready; при ложном passed координатор обязан отклонить содержание независимо от валидности формата.
