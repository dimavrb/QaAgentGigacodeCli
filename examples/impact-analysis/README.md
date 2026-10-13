# Учебный выбор регресса

Синтетический пример без GIGACODE, CI или стенда. Изменён orders, export зависит от orders. TC-LOGIN обязателен, TC-NEW новый, TC-FAIL ранее падал. TC-UNKNOWN не имеет подтверждённой связи и остаётся в прогоне с review. TC-PAY исключается по учебной модели: получается 6 из 7 сценариев. Это проверка механики, а не измерение ускорения продукта.

```bash
python3 examples/impact-analysis/build_demo.py --output-dir /tmp/qa-impact-demo
python3 scripts/validate_qa_result.py --input /tmp/qa-impact-demo/result.json --task-id QA-IMPACT --request-id QA-IMPACT-1 --agent qa-impact-analyst --impact-input /tmp/qa-impact-demo/impact-input.json --require-self-review --self-review-assignment /tmp/qa-impact-demo/assignment.json
```

Каталог, история, требования и changes.json сохранены с контрольными суммами. При изменении входа обновляйте соответствующую ссылку/sha256 до нового назначения, не исправляйте принятую версию. build_demo и селектор не перезаписывают существующие результаты. Для повтора выбирайте новый каталог.

Для проверки реестра:

```bash
python3 scripts/manage_task.py --state /tmp/qa-impact-demo/task.json init --task-id QA-IMPACT --title "Учебный импакт-анализ" --criteria /tmp/qa-impact-demo/criteria.json
python3 scripts/manage_task.py --state /tmp/qa-impact-demo/task.json assign --input /tmp/qa-impact-demo/assignment.json --expected-revision 1
python3 scripts/manage_task.py --state /tmp/qa-impact-demo/task.json prepare --request-id QA-IMPACT-1 --expected-revision 2
```

Дальше фактический start с owner_task_id и delivery_id из prepare, submit и независимое review по TASK_EXCHANGE.md. Не приписывайте сохранённому примеру запуск живой роли или готовность релиза. criteria.json требует отдельного qa-result-reviewer по impact_selection, этот reviewer также получает первичные snapshots и docs/IMPACT_ANALYSIS.md. Без принятого независимого ревью критерий задачи не закрывается.

Неполная dependencies/test_mapping/history, неизвестное изменение или пробел покрытия переводят рекомендацию в полный набор. Изменение frozen requirements.md отклоняется даже при неизменных manifest и каталоге. Все наборы параметризованных кейсов сохраняются.
