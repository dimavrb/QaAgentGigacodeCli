# Учебный отчёт по бизнес-кейсам и тогглам

Все данные синтетические; приложение, CI и браузер не запускались. Scope связывает оплату/отмену с тремя checks: оплата при new_checkout=true и false, отмена при true. Один записанный прогон содержит успешную оплату при true и blocked отмену. Для false нет запуска. Оба бизнес-кейса должны остаться incomplete, при этом факт успешной оплаты и его учебное ревью сохраняются.

```bash
QA_REPORT_WORKDIR=$(mktemp -d)
python3 scripts/build_test_report.py --scope examples/test-reporting/scope.json --runs examples/test-reporting/runs.json --revision demo-v1 --output-dir "$QA_REPORT_WORKDIR/report-v1"
python3 scripts/build_test_report.py --check "$QA_REPORT_WORKDIR/report-v1"
```

Прочитай report.md. Expected counts: planned=3, executed=1, target_executed=1, passed=1, failed=0, accepted_passed=1. PAY-OFF не запускался, CANCEL-ON заблокирован. Решение о выпуске not_assessed. Изменённый файл evidence/snapshot, неверные счётчики или ручная правка Markdown должны отклоняться проверкой.

review.json — учебная запись согласованности с fingerprints, не приёмка продукта. Не переноси её статус passed или примерные значения в рабочую задачу. Правила — docs/TEST_REPORTING.md; регрессии — tests/test_test_reporting.py.

Для примера с конкретными действиями и наблюдением используй `--runs examples/test-reporting/detailed-runs.json` и новый каталог/ревизию. В нём отдельные detailed-feature/evidence/review-файлы; исходные краткие записи не переписаны. Числа и пробелы те же: new_checkout=OFF не проверен, отмена/возврат заблокированы. Учебная карточка заказа показывает «оплачен»; проверка списания и чека из этой записи не следует. В report.md бизнес-цель, правило, действия и факт видны без перехода к сценариям.
