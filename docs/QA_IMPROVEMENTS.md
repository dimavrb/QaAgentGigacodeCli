# Контроль оценщика и измеримое ускорение QA

Включено в поставку 2.1. CLI: `python3 scripts/qa_improvements.py --help`.
Команды создают новый файл; `verify --report <файл>` пересчитывает его по первичным
источникам. Неизвестная схема, дубликаты ключей и невалидные числа отвергаются.
Содержательное независимое ревью источников остаётся обязательным.

## Контроль смысловой оценки

`examples/grader-controls/suite.json` содержит пары правильных/ошибочных ответов:
переданный норматив, отрицание, слабый assert, выдуманный запуск, неизвестное
ожидание, частичный успех, другая сборка, инструкция внутри оцениваемого ответа.
Это новый учебный набор, не восстановленные первичные материалы A0. Разметка
предварительная (`reviewed_by=null`); эксперт проверяет её до квалификации оценщика.
Не вписывайте имя эксперта без ревью. Дополняйте набор реальными инцидентами.
Отдельные calibration/heldout случаи сохраняйте; после использования heldout
для настройки заменяйте его. Повторные живые оценки остаются обязательными.

```sh
python3 scripts/qa_improvements.py controls-prepare --suite examples/grader-controls/suite.json --output /tmp/controls-public.json
python3 scripts/qa_improvements.py controls-run --pack /tmp/controls-public.json --output /tmp/controls-attempt.json --cli gigacode --model <модель>
python3 scripts/qa_improvements.py controls-check --suite examples/grader-controls/suite.json --pack /tmp/controls-public.json --review /tmp/controls-attempt.json --output /tmp/controls-score.json
python3 scripts/qa_improvements.py verify --report /tmp/controls-score.json
```

Оценщик получает только public-копию: без gold_status/reason, error_family, split,
reviewed_by и пути набора. GRADING_SYSTEM/invoke_isolated общие со штатным
run_benchmark_grader: bare, пустой cwd, отключённые инструменты. Сохраняются raw,
модель в запросе, хеш инструкции, время/usage даже при пустом ответе. Итог сверяет
вердикты с эталоном, дословность цитат, полноту ID и raw; считает ложные одобрения,
блокировки, воздержания и долю обнаруженных ошибок отдельно по calibration/heldout.
Реальная цитата не доказывает правильный смысл verdict. rc: 0 — все verdict
совпали, 1 — расхождения/провал исполнения, 2 — ошибка источника/структуры.
Учебный запуск указывайте `--measurement-kind fixture`; promotion_eligible=false.

## Обновление карты покрытия

Коллектор экспортирует JSON schema_version=1: revision, environment_id,
test_data_id, collection_id, catalog_sha256, dependencies_complete/history_complete
(boolean), components и tests. Компонент: component_id, depends_on, files (точные
относительные пути). Тест: scenario_id, component_ids, coverage_complete (boolean),
attempts=[{attempt_id,status:pass|fail|blocked|error,duration_seconds}]. Пример создаёт
`examples/qa-improvements/build_demo.py`. Адаптер реального коллектора передаёт
наблюдённые связи и ограничения; универсальный парсер JaCoCo/coverage/trace не заявлен.

```sh
python3 scripts/qa_improvements.py map --input <impact-input.json> --observations <coverage.json> --environment-id <стенд> --test-data-id <данные> --output <новый-impact-input.json>
python3 scripts/select_regression.py --input <новый-impact-input.json> --output <selection.json>
```

Стенд/данные сверяются с аргументами координатора, revision с base/target,
каталог по хешу. Несвежие данные, отсутствующий тест, неполное покрытие или путь
изменения без владельца расширяют прогон. Ненаблюдённый вызов не доказывает отсутствия
связи. Длительность — медиана попыток; провал до повторного успеха сохраняет
previously_failed. Обязательная политика не сужается. Новый input включает хеши
и абсолютные источники, execution_context/coverage_binding; смена ревизии,
каталога или контекста даёт fallback. Полнота инструментирования требует reviewer.

## Пилот полного и выбранного регресса

```sh
python3 scripts/qa_improvements.py shadow --input <impact-input.json> --full <full-results.json> --selected <selected-results.json> --output <shadow.json>
```

Два фактических исполнения одной сборки, каталога, стенда и снимка данных;
разные run_id. Full содержит все ID, selected ровно run_scenario_ids. Фильтрация
результатов full не является вторым исполнением. Run schema_version=1: run_id,
build_revision, environment_id, test_data_id, catalog_sha256, measurement_kind,
results и timing. Result: scenario_id и полный attempts. Попытка: attempt_id,
status, duration_seconds; при неуспехе classification=confirmed_defect|infrastructure|
unclassified. Для confirmed_defect обязательны defect_id и независимое
confirmation={path,sha256}. Необязательный capture={path,sha256} тоже проверяется.
Повторный успех не стирает провал. Отчёт находит пропущенные defect_id, сохраняет
нестабильные случаи, инфраструктуру и дефекты только выбранного прогона.
Неразобранные ошибки/неполное время дают inconclusive. Разница времени может быть
отрицательной. no_miss_in_observed_run относится только к этому прогону;
promotion_eligible=false. Нужны серия изменений, прошлые дефекты, периодический full,
условие отката. Учебный demo не доказывает ускорения продукта.

## Сквозное время

Run.timing.plan/trace — {path,sha256}. Независимый plan schema_version=1: run_id,
build_revision, environment_id, test_data_id, required_phase_ids. Trace с той же
идентичностью: started_at, accepted_at (epoch seconds либо null), spans. Span:
span_id, phase_id, kind=active|queue|human_wait, started_at/finished_at,
status=succeeded|failed|cancelled. Active: уникальный attempt_id, retry,
input_tokens/output_tokens (null, если не измерены). Включайте анализ, автора,
все reviewer, координатора, setup, очередь, инструменты, повторы. Пропущенный этап
или интервал сохраняет complete_timing=false; схема не предполагает полноту.

```sh
python3 scripts/qa_improvements.py timing --plan <plan.json> --trace <trace.json> --output <timing.json>
python3 scripts/qa_improvements.py timing-import --plan <plan.json> --trace <trace.json> --index <metrics-index.json> --output <merged-trace.json>
```

Полное время — до приёмки. Активная работа — сумма времени исполнителей;
время учтённых интервалов — объединение, без сложения перекрытий в wall time.
Неизвестный usage не заменяется нулём. Worker сохраняет корреляцию task/trace/
request/attempt/step, retry, epoch-границы в metrics.json. Импорт требует plan.task_id/
trace_id, step_phases={step_id:phase_id}; index.metrics=[{path,sha256}]. Подсчёт
сверяет импортированные spans с первичными metrics. Очередь, человек и интерактивный
координатор остаются явными наблюдениями. Все провалы/повторы входят в затраты.

## План и исполнение

```sh
python3 scripts/qa_improvements.py schedule --input <impact-input.json> --policy <execution-policy.json> --output <schedule.json>
python3 scripts/qa_improvements.py execute-plan --input <impact-input.json> --policy <execution-policy.json> --commands <commands.json> --output-dir <новый-каталог>
```

Policy: schema_version, impact_input_sha256, workers=[{worker_id,capabilities}],
resources=[{resource_id,capacity}], tests ровно всех ID: scenario_id,
required_capabilities, resource_ids, isolated_test_data. Общие данные без блокировки
отвергаются. Длинные тесты планируются раньше с учётом workers/resources; это
эвристика. Нет длительности — needs_durations и null, без выдуманной оценки.
Commands: schema_version, impact_input_sha256, run_id/build_revision/environment_id/
test_data_id, measurement_kind, tests ровно выбранных ID: scenario_id, argv (список,
без shell), cwd, timeout_seconds, max_attempts=1..3, retry_exit_codes. Повторы:
retry_safe=true, сброс состояния самим тестом/обёрткой. Команды утверждает координатор;
impact-роль только планирует. Один поток на worker, семафоры во всех попытках;
timeout завершает группу процессов. Блокировки локальны исполнителю; другие CI jobs
требуют внешних блокировок. Выход: schedule, stdout/stderr, все попытки, execution-trace,
results с qa_accepted=false/timing=null. Завершение процессов не является приёмкой QA.
Перед shadow координатор разбирает провалы и добавляет timing plan/trace полного
маршрута. Schedule исключает setup/очередь; фактическую разницу измеряет shadow.

## Принадлежность гейтов маршруту

Назначение improvement_plan: независимый JSON schema_version=1, session_id,
reports=[{report_id,command,command_inputs,requirements}]. command:
controls-check|shadow|schedule|timing; command_inputs — пути команды относительно
plan. Пороги controls-check: require_live/require_reviewed_gold,
max_false_approvals/max_false_blocks/max_abstentions; shadow: require_live/require_no_miss;
schedule: require_planned; timing: require_complete. Координатор явно задаёт пороги.
Результат payload.improvement_reports={report_id:путь}, summary точно объединяет
summary_text в порядке plan. Реестр хеширует первичные входы/выходы; валидатор с
независимым --improvement-plan пересчитывает всё и запрещает ready при нарушении
порогов. Blocked сохраняет результаты, но не закрывает этап. Отчёты без плана
отвергаются. Назначения improvement и benchmark_summary имеют отдельные сводки.
Самопроверка и содержательное независимое ревью продолжают действовать.
