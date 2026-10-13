# Учебный журнал сбоев

Все записи синтетические. Модель, продукт и стенд не запускались.

```bash
QA_FAILURE_DEMO_DIR=$(mktemp -d)
python3 scripts/manage_failure_journal.py record --journal "$QA_FAILURE_DEMO_DIR/failures.jsonl" --entry examples/failure-journal/01-open.json
python3 scripts/manage_failure_journal.py record --journal "$QA_FAILURE_DEMO_DIR/failures.jsonl" --entry examples/failure-journal/02-recovered.json
python3 scripts/manage_failure_journal.py record --journal "$QA_FAILURE_DEMO_DIR/failures.jsonl" --entry examples/failure-journal/03-hypothesis.json
python3 scripts/manage_failure_journal.py check --journal "$QA_FAILURE_DEMO_DIR/failures.jsonl"
python3 scripts/manage_failure_journal.py summarize --journal "$QA_FAILURE_DEMO_DIR/failures.jsonl" --scope-kind synthetic --output-dir "$QA_FAILURE_DEMO_DIR/summary"
python3 scripts/manage_failure_journal.py find --journal "$QA_FAILURE_DEMO_DIR/failures.jsonl" --scope-kind synthetic --query "отмена сервис" --context-file examples/failure-journal/search-context.json --limit 3
```

Ожидается 3 записи истории и 2 инцидента: один восстановлен, один открыт. Общий блокер двух сценариев считается один раз. environment имеет записанное подтверждение; requirements остаётся гипотезой. operational-сводка этого журнала пуста.

Повторная отправка 01-open.json не добавляет запись. Изменение доказательства не меняет историю: check выдаёт ограничение, сводка не показывает неподтверждаемое восстановление зелёным. Правила — docs/FAILURE_JOURNAL.md.

find возвращает один кандидат DEMO-1-recovered с context_match=matched, проверенными ссылками и требованием содержательной оценки применимости. По умолчанию operational эта учебная запись не возвращается. Изменённое доказательство или повторно открытый инцидент убирает решение из выдачи. Это сохранённый синтетический случай, не инструкция перезапустить реальный сервис.
