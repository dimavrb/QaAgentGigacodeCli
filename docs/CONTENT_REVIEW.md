# Независимое ревью и ограниченное исправление

Поставка 2.0 поддерживает связанное ревью QA-артефактов и автономный цикл
одного автора с одним/двумя reviewer. Используются существующие реестр,
supervisor, отмена, проверки хешей и лимиты. Архив 1.9 этим изменением не обновлён.

## Назначение reviewer

Координатор выполняет COORDINATION_SAFETY.md: замороженные inputs без чужой
записи, абсолютный requirement_coverage.source, файл/JSON результата и owner
по runtime. Недоступный роли валидатор/SHA256 выполняет он же с квитанцией.
Findings.blocking — JSON bool; changes_requested требует blocking=true,
review_passed — всех checks passed без blocking.

Разделённый план проверяй по [TEST_PLAN_PARTITION.md](TEST_PLAN_PARTITION.md).
У reviewer группы та же plan_partition и её scenario_ids/первичные фрагменты.
Общий результат требует отдельной проверки межгрупповых зависимостей, единых
ролей/состояний/лимитов, смысловых дублей и полного покрытия. Структурный merge
не является содержательным принятием. Verdict группы не подтверждает всю задачу.

Для тест-плана проверь связи requirement_coverage и точные source по
[TEST_PLAN_HTML.md](TEST_PLAN_HTML.md): локальный файл/якорь доступен и включён
в ревизию, либо передана прямая HTTP(S)-ссылка. HTML формируется после принятия
и отражает эти связи; наличие ссылки не доказывает фактическое покрытие требования.

qa-result-reviewer оценивает планы, анализ и результаты по назначенному направлению.
qa-autotest-reviewer сохраняет обязательное специализированное ревью автотестов.
При ревью ручного каталога применяй [TEST_CASE_REVIEW.md](TEST_CASE_REVIEW.md):
используй выбранный API/UI/Kafka-пример для оформления, а первичные требования —
для смысла. Для прямого managed-назначения scope=test_cases включает стандарт
автоматически; test_type и instruction_paths выбирают пример. Сверь текущую
структурную проверку catalog_validations с путём и хешем target. Она не заменяет
проверку достижимости, данных и различающих ожиданий и не даёт новых tools.
qa-conclusion-reviewer проверяет существенные утверждения итогового ответа по первичным источникам. Required_reviews.conclusions закрепляет /summary и назначенные JSON-поля; conclusion_checks связывает каждое точное значение с цитатами источников. Маршрут, ограничения и локальный пример — [CONCLUSION_REVIEW.md](CONCLUSION_REVIEW.md).

Два qa-conclusion-reviewer могут обсуждать конкретные расхождения после независимых оценок: discussion.max_rounds=1..2, новый доказательный отзыв каждого направления, сохранённые первичные verdict и ограничения. Приёмка требует одну фазу/пакет обоих направлений; первоначальное blocking-замечание на прежнем target сохраняется. Формат и пример — [REVIEW_DISCUSSION.md](REVIEW_DISCUSSION.md).
Для нового протокола оба получают review_target:

```json
{
  "review_target": {
    "request_id": "author-1",
    "result_path": "/absolute/path/author.json",
    "artifact_hashes": {"/absolute/path/author.json": "64 hex SHA-256 characters"},
    "scope": "oracle"
  },
  "review_criteria_ids": ["C-FILTER"]
}
```

Назначение reviewer содержит все target-файлы в inputs; не добавляй отчёт другого
reviewer в его входы. Цель может находиться в reported: это разрешает ревью до
приёмки автора и не превращает её в готовую зависимость исполнения. Назначение
проверяет соответствие зарегистрированному результату; submit перепроверяет
входы, target-хеши и привязку payload. Reviewer запускается отдельным owner_task_id.

Payload нового ревью: target_request_id, target_sha256 (result_path), review_scope,
checks, findings, limitations. Checks покрывают ровно review_criteria_ids:

```json
{
  "criterion_id": "C-FILTER",
  "status": "failed",
  "note": "Непустая выдача допускает чужой заказ",
  "evidence": ["/absolute/path/counterexample-run.txt"]
}
```

Finding: finding_id, criterion_id, blocking (bool), problem, evidence (точная ссылка
или цитируемое наблюдение), resolution_condition. review_passed требует passed
всех checks без blocking findings. changes_requested требует конкретного blocking
finding. При недоступном ожидании/наблюдении — blocked с неизвестным check и
нужным условием. Старый контракт qa-autotest-reviewer без review_target совместим;
новое связанное ревью дополнительно сохраняет reviewed_revision автора.

Локальные evidence из checks автоматически проверяются и хешируются при submit,
даже без дублирования в artifacts; первичные target-входы уже хешируются.
HTTP-ссылка остаётся внешней ссылкой, её содержимое не зафиксировано: существенный
вывод требует локального снимка. Валидатор проверяет форму и привязку; смысл оценивает reviewer.
Принятие правдивого changes_requested означает сохранение ревью, а не согласие
на завершение задачи. Голосование не закрывает существенное замечание.

## Критерии приёмки

Старый required_review_agent сохраняется. Для одного/двух обязательных направлений
новый критерий задаёт required_reviews, например:

```json
{
  "criterion_id": "C-FILTER",
  "description": "Проверка обнаруживает чужую запись",
  "source": "requirements.md#R-1",
  "required": true,
  "required_reviews": [
    {"scope": "oracle", "agent": "qa-result-reviewer"},
    {"scope": "execution", "agent": "qa-autotest-reviewer"}
  ]
}
```

assess ссылается на автора и все обязательные reviewer. Для каждого выбранного
авторского результата требуются все обязательные направления; при продолжении
scenario_execution это каждый источник выбранных scenario_sources. Каждое направление требует
review_passed точного target, собственного владельца, отличного от автора и других
направлений, и passed для соответствующего criterion_id. Принятое blocking-ревью
того же target не исчезает от другого passing verdict; исправление получает новую
ревизию/request_id и новое ревью. Полномочия не расширяются при исправлении.

Этот запрет действует и до итоговой оценки критерия: принятое changes_requested
не позволяет принять прежнего автора, использовать его как ready-зависимость,
записать verified, создать/повторно использовать checkpoint или завершить задачу.
Позднее blocking-ревью перепроверяется на следующем переходе. Через context
можно передать уже принятый результат для диагностики и исправления; это не
подтверждает готовность исполнения. Принимать сам отчёт reviewer с замечаниями
и возвращать автору changes_requested по-прежнему разрешено.

Для kind=combination_coverage дополнительно требуются combination_checks для каждой
связи выбранного bundle, все passed. Каждый check содержит bundle_id, binding_id,
status, конкретный note об условиях/правиле/наблюдении и evidence. Модель заранее
закреплена model_sha256 критерия; generic passed не подтверждает это покрытие.
Неизвестные binding_id другого target и дубли отклоняются; вложенные evidence
хешируются. Обязательные направления отдельно оценивают весь выбранный bundle.
Описание формата и готовые примеры — [TEST_DESIGN.md](TEST_DESIGN.md).

## Автономный цикл

План schema_version=1 содержит author_plan (обычный managed execution план с одним
шагом), reviewers (scope, agent, необязательный adapter, max_wall_seconds),
max_corrections (0..5, по умолчанию 2), max_active_seconds (общий бюджет).
Для автоматизатора одно направление обязательно использует qa-autotest-reviewer.
Все required_reviews/required_review_agent критерия включаются в план до запуска.

```bash
python3 examples/review-cycle/build_demo.py --output-dir /tmp/qa-review-demo
python3 scripts/manage_review_cycle.py --state /tmp/qa-review-demo/cycle.json init --plan /tmp/qa-review-demo/plan.json
python3 scripts/manage_review_cycle.py --state /tmp/qa-review-demo/cycle.json serve --max-seconds 60
```

Локальный пример выполняет реальные unittest на синтетическом ответе. Слабый
assert пропускает чужую запись; независимые fixture-reviewer фиксируют это, автор
получает feedback и создаёт сильный assert, новый контрпример падает на нужной
проверке. Это проверка протокола и механизма assert, не поведения GIGACODE/продукта.

Каждое направление запускается в отдельном процессе/сессии без чужого verdict.
Цикл принимает сохранённые review, возвращает замечания автору и создаёт новую
ревизию. Исходные отчёты сохраняются; результаты/входы не перезаписываются.
Одинаковое замечание без нового evidence или исчерпанный бюджет останавливают цикл.
Технические retry supervisor отделены от содержательных исправлений.

Авторские blocked/needs_*/partial и неполное выполнение сохраняют работу и
останавливают автоматическую приёмку: недоступное ожидание нельзя «исправить»
голосованием. Координатор продолжает независимые назначения. Для отдельной задачи
о промежуточном отчёте используй обычный маршрут принятия документа, без объявления
полной продуктовой проверки.

accepted цикла означает принятие артефакта. assess и finish всей задачи остаются
отдельными проверками согласованного scope; supervise completed их не заменяет.
После завершения передавай result_request_id/review_request_ids из сохранённого
состояния, проверяя актуальность доказательств в реестре.

cancel сохраняется до явного resume --decision; бюджеты и число раундов не
сбрасываются. Незавершённые переходы исправления, cancel и resume сохраняются до
изменения дочерних состояний и повторяются идемпотентно. Каждый worker ограничен
остатком общего бюджета даже без нового tick. show читает состояние, tick продолжает
тот же цикл. serve ограничен 60
секундами на вызов; продолжай с тем же state. При blocked не создавай новый план
ради обхода исчерпанного бюджета: нужен новый факт/полномочие и решение координатора.

Command adapters — доверенные исполняемые команды; механизм не изолирует их от
хоста. GIGACODE сохраняет ограничения managed execution. Reviewer получает входные
снимки и при необходимости собственную gateway_policy; авторская не наследуется.
Для нового запуска required_reviews[].execution задаёт инструмент, аргументы и
наблюдение; execution_checks с OBS-квитанциями проверяется перед приёмкой автора,
на assess и finish. Старый лог/кеш, timeout и неизвестное не подтверждают запуск.
Формат, временная проверка backend-job и пример реального локального MCP-runner —
[REVIEW_EXECUTION.md](REVIEW_EXECUTION.md). Без инструмента критерий получает
unknown/blocked. Живой запуск не может объявлять мысленный контрпример выполненным экспериментом.
Бинарные inputs в headless prompt передаются как unavailable с хешем: текстовое
ревью не подтверждает невидимый скриншот. Реальный GIGACODE/MCP цикл ещё требует замера.

## Гейты benchmark-оценки

Для оценки QA-агента обязателен BENCHMARK_EVIDENCE_GATES.md: отдельный пакет копий вместо каталога прогона, tool-free оценивающая роль, проверка цитат и приёмка сохранённого файла по абсолютному пути. Уведомление не является данными. Штатные make/check_benchmark_grading_pack, run_benchmark_grader и accept_benchmark_review принадлежат маршруту, а не усмотрению reviewer. Готовая сводка требует build/check_benchmark_summary текущей evidence_session_id. Перед закрытием проверяются схема, все ожидаемые записи и первичные файлы. Исторические ручные ведомости допускают аудит --verify-quotes, но не закрытие нового этапа.
