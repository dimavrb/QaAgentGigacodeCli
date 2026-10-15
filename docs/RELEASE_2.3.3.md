# QA-пакет 2.3.3 — подтверждения переходов оркестратора

Принятое blocking-ревью текущего target теперь запрещает принять прежнего
автора и передать его как ready-зависимость. Проверка выполняется также для
поздних замечаний на assign/prepare/start/submit/review, при verified/finish
и перед созданием или повторным использованием checkpoint supervisor.
Другой passing verdict не отменяет замечание. Диагностический context,
принятие достоверного отчёта reviewer и возврат замечаний автору сохраняются.
Исправление получает новый request_id, отдельный результат и новое ревью.

Для scenario_execution можно сохранить failed, unknown и not_applicable без
UnboundLocalError. Обязательные failed/unknown по-прежнему запрещают finish;
неприменимость требует доказательства с повторной проверкой хешей. Ошибка
одной записи assessments отклоняет весь пакет.

Проверки используют локальные синтетические артефакты и процессы. Они не
подтверждают качество живой модели GIGACODE или внешние интеграции.

Полный набор: **817 тестов, OK**. Добавлены 13 регрессионных тестов:
поздние замечания на переходах реестра, запрет прежнего автора даже после
другого passing verdict, повторное использование источника из кеша,
диагностический context, исправленная версия с новым ревью, создание и
восстановление checkpoint, все оценки исполнения, атомарность assessments
и повторная проверка доказательств неприменимости.

```bash
python3 -m unittest discover -s tests -p test_task_exchange.py -v
python3 -m unittest discover -s tests -p test_check_quality.py -v
python3 -m unittest discover -s tests -p test_second_package.py -v
python3 -m unittest discover -s tests -v
python3 scripts/package_agent.py --output-dir /tmp/qa-agent-2.3.3 --date 2026-10-15
python3 scripts/package_agent.py --verify /tmp/qa-agent-2.3.3/qa-agent-2.3.3.zip
```

Обновление — [UPGRADE_2.3.3.md](UPGRADE_2.3.3.md).
