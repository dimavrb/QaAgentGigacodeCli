# Контракт QA-работы

Этот документ задаёт формат взаимодействия. Конфигурация GIGACODE CLI сама по себе не гарантирует соблюдение полей: в новом маршруте их проверяет программная передача по TASK_EXCHANGE.md через существующий полный валидатор.

## Оболочка сообщения

```json
{
  "task_id": "QA-123",
  "request_id": "QA-123-data-1",
  "agent": "qa-test-data-builder",
  "event": "data_ready",
  "summary": "Предусловия подготовлены и проверены",
  "artifacts": ["path-or-url"],
  "blockers": [],
  "payload": {}
}
```

`task_id` связывает все этапы одной задачи. `request_id` идентифицирует отдельное назначение; повторная доставка того же результата не должна запускать новый этап. `artifacts` содержит ссылки на первичные материалы, а не их длинную копию в сообщении.

При отдельном запуске автотестов ответ должен быть JSON-объектом в этой оболочке. Валидатор проверяет соответствие `task_id`, `request_id`, `agent`, допустимость события для роли и поля результата ниже. Для `blocked` заполни `blockers`; для `needs_*` дай конкретный запрос в `payload`. Неполный ответ нельзя считать успешным этапом.

## Краткое назначение исполнителю

Для новых назначений до assign выполняй COORDINATION_SAFETY.md: все локальные
пути абсолютны, в том числе result_path, artifact_base, inputs/artifacts,
payload.sources_reviewed[] и payload.requirement_coverage[].source. HTTP(S)
остаётся URL, локальный source сохраняет точный якорь. Если payload ещё нет,
в назначении фиксируется абсолютная карта источников; до submit проверяется
готовый payload целиком. Схема/legacy-CLI могут поддерживать относительные пути,
но новый рабочий протокол их не выпускает. Исторические данные не мигрируются.
Inputs не пересекаются с публикуемыми файлами самого назначения и активных
чужих владельцев; для изменяемой базы передаётся побайтово проверенный snapshot
принятой ревизии. Наличие result_path в тексте не доказывает запись результата.

Передавай `task_id`, уникальный `request_id`, цель и критерии готовности, а также `stop_condition` (когда остановить выполнение и вернуть управление) и `expected_artifact` (какой проверяемый результат сохранить). Добавляй только применимые к роли сведения: идентификаторы сценариев, подтверждённое ожидаемое поведение и его источник, окружение и версию, необходимые предусловия, открытые вопросы. Для больших материалов указывай доступный исполнителю путь или URL и нужный раздел. Не включай весь предыдущий диалог или полные логи. Если источник недоступен, процитируй минимальный необходимый фрагмент и обозначь его происхождение.

Ответ содержит только результат назначения, ограничения и ссылки на доказательства. При повторной передаче используй новый `request_id` и укажи, что изменилось относительно предыдущего результата. Не дублируй неизменившиеся материалы.

При создании тест-кейсов передавай `test_type: api`, `test_type: ui` или `test_type: kafka` и цель проверки, например «валидация обязательных полей». Для смешанной задачи укажи тип каждой группы сценариев; отдельный исполнитель для каждой группы не обязателен. Если тип очевиден из задания, исполнитель может определить его сам. Навык `qa-test-cases` читает индекс только выбранного типа и подходящий пример; координатор не копирует примеры в назначение. Для API, UI и Kafka применяй Шаг — Ожидаемый результат — Тест-дата по TEST_CASE_STYLE.md; примеры выбираются из examples/test-cases/<test_type>. HTTP-данные хранятся структурированно в request/response, без копирования в текстовый test_data; для UI текстовое test_data содержит значения полей, выбранные объекты и ожидаемые строки/тексты. Обязательные поля результата и schema_version каталога остаются прежними.

## Понятные служебные сообщения

Для UI через MCP назначение дополнительно содержит общий абсолютный browser_pool_path, доступный manage_browser_pool.py и фактический GIGACODE owner_task_id. Исполнитель получает один lease непосредственно перед UI и передаёт в artifacts журнал владения/cleanup (server, lease_id, владелец, времена, закрытие либо карантин, первичный результат/hash). Формат QA-события не меняется. Координатор проверяет освобождение по BROWSER_PROFILES.md отдельно от продуктового результата; при прерывании принимает cleanup. Для анализа/API/обычных fixtures автотестов эти поля не требуются.

В `summary` пиши одну-две фразы с конкретным поведением или блокером и степенью подтверждения. Для проблемы сообщи влияние, если оно установлено; для вопроса — что он блокирует и какой ответ нужен. Статусы, числа и выводы должны соответствовать `payload` и доказательствам.

Координатор преобразует отчёт в понятное сообщение пользователю: суть → влияние → подтверждение → нужное решение. Полные шаги, логи и история остаются в артефактах. Не выводи весь JSON и не начинай с технического статуса вместо сути. Форматы для дефекта, вопроса, стенда, инструмента, успеха и ревью — в `docs/MESSAGES.md`.

Вопросы пользователю и ответы на них оформляются отдельными артефактами по `docs/QUESTIONS_UI.md`: HTML-форма, пакет с ID задачи/назначения/вопросов и единый JSON ответов с ревизией пакета. Это не новое QA-событие. Ответы применяет координатор после проверки; `deferred` не закрывает исходный вопрос, `answered` требует содержательной оценки и сохранения источника решения.

Краткость не отменяет обязательные поля, источники, ограничения и доказательства. Сохраняй условия, отрицания и различие факта, гипотезы и неизвестного. Воспроизведение исполнителем не заменяет содержательное принятие координатором. Схема JSON-отчёта не меняется.

## Состояние задачи

Для новых задач единая запись, назначения, проверяемая передача и события готовности ведутся через `scripts/manage_task.py` по `docs/TASK_EXCHANGE.md`. Полный валидатор вызывается программно при submit/review/prepare зависимого назначения. `reported` означает прошедшую структурную проверку, `accepted` — записанную содержательную оценку координатора; принятие блокера не снимает его. Источники и назначения связаны ID, а локальные материалы — контрольными суммами. Специализированные состояния расследования/runner/браузеров сохраняют детали и передаются ссылками. Оболочка QA-отчёта не меняется.

Храни раздельно:

- `execution_status`: `queued`, `running`, `waiting`, `completed`, `failed`.
- `qa_outcome`: `passed`, `defect_confirmed`, `not_reproduced`, `expected_behavior`, `inconclusive`, `not_applicable`.
- `review_status`: `not_required`, `pending`, `changes_requested`, `passed`.

Завершённое расследование может иметь `qa_outcome=not_reproduced`. Написанный тест может иметь `execution_status=waiting`, если запуск или ревью заблокированы.

Для расследований состояние, продолжение и содержательное принятие описаны в `docs/INVESTIGATION.md`. Старые отчёты сохраняют структурную совместимость; их валидность по форме не является принятием вывода. В новом процессе координатор передаёт путь состояния и проверяет его вместе с записанным ревью через `--investigation-state`. Добавочное `payload.attempt_ids` обязательно при этой проверке. Для новых подозрений включается `verification_required: true` и `payload.verification_artifact` по `docs/DEFECT_VERIFICATION.md`; этот отчёт всегда проверяется с состоянием. Прежние события и поля сохраняются.

## События и обязательные данные

| Событие | Обязательные ключи в `payload` |
| --- | --- |
| `analysis_ready` | `requirements`, `risk_scenarios`, `blocking_questions`, `assumptions`, `coverage_gaps`, `test_plan_required`, `test_plan_reason` |
| `impact_ready` | `impact_artifact`, `impact_input`, `input_sha256`, `base_revision`, `target_revision`, `run_scenario_ids`, `review_scenario_ids`, `excluded_scenario_ids`, `limitations`; независимый --impact-input обязателен |
| `needs_analysis`, `needs_environment`, `needs_test_data`, `needs_test_plan` | Конкретный вопрос или недостающее условие и затронутые сценарии |
| `coverage_ready` | `plan_artifact`, `plan_revision`, `requirement_coverage`, `open_questions`; ранняя карта без подробного каталога |
| `plan_partial` | Те же поля плюс `test_cases_artifact`; готовая часть и явные блокеры проектирования |
| `plan_ready` | `plan_artifact`, `plan_revision`, `test_cases_artifact`, `requirement_coverage`, `open_questions`; `plan_artifact` — краткий план с покрытием по ID без повторения шагов кейсов; `test_cases_artifact` — абсолютный локальный JSON-путь нового результата, проверенный по `docs/TEST_CASE_STYLE.md`; относительные пути старых данных остаются режимом совместимости |
| `environment_ready` / `environment_partial` | `environment_id`, `product_version`, `verified_capabilities`, `limitations`; в новых назначениях ещё `environment_assignment_sha256`, `guide_checks` по ENVIRONMENT_READINESS.md; для partial ещё `ready_scenarios`, `blocked_scenarios` |
| `data_ready` / `data_partial` | `environment_id`, `data_ids` или `recipe`, `verified_properties`, `cleanup`, `scenario_ids`; для partial ещё `blocked_scenarios`; новые назначения — product_version, data_assignment_sha256, data_state и проверки каждого условия по DATA_READINESS.md |
| `stubs_plan_ready` | `environment_id`, `provider`, `mcp_server`, `project_id`, `namespace`, `assignment_artifact`, `scenario_ids`, `limitations`; черновик без мутаций |
| `stubs_configured` | Контекст и `profile_artifact`, `verification_artifact`, `configured_scenarios`, `blocked_scenarios`, `cleanup`, `limitations`; конфигурация не означает готовность исполнения |
| `stubs_ready` / `stubs_partial` | `environment_id`, `provider`, `mcp_server`, `project_id`, `namespace`, `profile_artifact`, `verification_artifact`, `scenario_ids`, `blocked_scenarios`, `cleanup`, `limitations`; приёмка с внешним --stub-assignment по STUBS.md |
| `investigation_complete` | `outcome`, `expected_source`, `actual_result`, `attempts`, `evidence`, `limitations`; для новых подозрений — `verification_artifact` по DEFECT_VERIFICATION.md и проверка с состоянием |
| `feature_test_complete` | `scenario_results`, `uncovered_scenarios`, `defects`; каждый результат содержит `scenario_id`, `environment_id`, `product_version`, `status`, `steps`, `expected_source`, `expected_result`, `actual_result`, `evidence` |
| `ready_for_review` | `scenario_id`, `requirement_source`, `base_revision`, `current_revision`, `changed_files`, `diff_artifact`, `run_result`, `limitations` |
| `automation_complete` | `coverage_reference`, `run_result`, `reason_no_change` |
| `review_passed` / `changes_requested` | `reviewed_revision`, `findings`, `checks`, `limitations` |
| `jira_defect_created` | `issue_key`, `issue_url`, `duplicate_check`, `evidence` |
| `jira_duplicate_found` | `issue_url`, `duplicate_check` |
| `test_report_ready` | `report_artifact`, `summary_artifact`, `report_revision`, `limitations`; report.json и report.md одного проверенного снимка перечислены в artifacts |
| `blocked` | В `blockers`: препятствие и недостающие данные; в `payload`: выполненные попытки и затронутые этапы, если известны |

Поля со множественным числом (`requirements`, `risk_scenarios`, `blocking_questions`, `assumptions`, `coverage_gaps`, `requirement_coverage`, `open_questions`, `verified_capabilities`, `limitations`, `ready_scenarios`, `blocked_scenarios`, `data_ids`, `verified_properties`, `scenario_ids`, `attempts`, `evidence`, `scenario_results`, `uncovered_scenarios`, `defects`, `changed_files`, `findings`, `checks`) — массивы. Структура аналитика и её исключения описаны ниже. Прочие перечисленные ключи — непустые строки, кроме `run_result` у `ready_for_review`, где допустим структурированный отчёт. Для `investigation_complete.outcome` используй `defect_confirmed`, `not_reproduced`, `expected_behavior` или `inconclusive`. Массивы подробных результатов могут храниться в артефакте, если в сообщении дана ссылка и краткое резюме.

Для `feature_test_complete` и `blocked` роли `qa-feature-tester` `scenario_results` содержит по одному объекту на каждый назначенный ID; повторяющиеся и пропущенные ID недопустимы. `status` принимает `passed`, `failed`, `blocked` или `not_run`. Для выполненных сценариев `steps` и `evidence` не пусты. `uncovered_scenarios` точно перечисляет ID со статусами `blocked` и `not_run`. При проверке сохранённого результата передавай назначенные ID через `--scenario-id` валидатору, иначе полноту относительно задания подтвердить нельзя.

Для `plan_ready` массив `requirement_coverage` кратко связывает критерии или риски с ID из `test_cases_artifact` либо явно обосновывает исключение. Незавершённые части передаются через `coverage_ready` или `plan_partial`. Не копируй сюда кейсы целиком; `plan_artifact` и `test_cases_artifact` передаются отдельными ссылками.

Общий блокер не удаляет назначенные сценарии из результатов и не превращает их в успех. Правила группировки блокеров, продолжения и применимости прежних результатов — в `docs/EXECUTION.md`; новые поля JSON для этого не требуются. Причина и ссылка на блокер помещаются в существующие поля и артефакты.

## Результат импакт-анализа

qa-impact-analyst возвращает impact_ready либо blocked. impact_ready — готовая рекомендация, включая полный fallback, а не исполнение или одобрение релиза. impact_artifact — локальный JSON, impact_input — снимок координатора, input_sha256 — его SHA-256. base_revision/target_revision и списки run_scenario_ids/review_scenario_ids/excluded_scenario_ids точно совпадают с пересчитанным артефактом. limitations — массив строк. Review-ID входят в run, run/excluded не пересекаются и охватывают весь каталог. Новые/обязательные/ранее падавшие проверки не исключаются.

Проверяйте по IMPACT_ANALYSIS.md с --impact-input из назначения. Без независимого снимка валидатор отклоняет рекомендацию. Снимки change_set/catalog, хеши, зависимости, причины fallback, coverage_gaps и дополнения проверяются в артефакте. Координатор и qa-result-reviewer отдельно проверяют полноту источников и исключения. Пробелы не исчезают после успешного расчёта. Ready-зависимость передаёт только run_scenario_ids, context позволяет читать весь отчёт. blocked сохраняет общий формат и не разрешает сокращённый прогон.

## Частичное выполнение и готовность

`feature_test_complete` содержит полную матрицу назначенного набора, включая смешанные результаты и `not_run`. `blocked` роли `qa-feature-tester` допустим только при подтверждённой блокировке всех назначенных сценариев: в `payload` обязательны те же `scenario_results`, `uncovered_scenarios`, `defects`, все результаты имеют статус `blocked`. Нехватка времени — `not_run`, а не техническая блокировка. В остальных ролях событие `blocked` сохраняет прежний формат.

В `environment_partial` списки `ready_scenarios` и `blocked_scenarios`, в `data_partial` списки `scenario_ids` и `blocked_scenarios` состоят из уникальных непустых строковых ID, не пересекаются и вместе точно совпадают с назначением. В `data_ready` список `scenario_ids` точно совпадает с назначением. Передавай полный назначенный набор валидатору через повторяемый `--scenario-id`. Без него программная проверка принадлежности назначению невозможна.

Новые назначения qa-test-data-builder требуют независимый data_assignment по [DATA_READINESS.md](DATA_READINESS.md). verified_properties содержит каждый requirement_id с фактическим результатом, статусом, временем и локальными доказательствами. Готовность вычисляется по всем свойствам, связям и видимости; data_state=cleaned/unknown и истёкший срок исключают ready/partial. data_ids состоят из уникальных непустых строк. Без scope можно сообщить blocked; историческая структура без него не разрешает новый ready-переход.

Новые назначения qa-jira-defect-creator требуют jira_assignment по [JIRA_PUBLICATION.md](JIRA_PUBLICATION.md) и точный принятый state-backed defect_confirmed как ready-зависимость. Оба успешных события содержат issue_key, issue_url, duplicate_check, evidence, jira_assignment_sha256 и publication_artifact. Поиск должен быть полным, кандидаты прочитаны/оценены; created требует квитанции одного create и чтения согласованных полей, duplicate_found — подтверждённого совпадения без write. Неопределённый исход остаётся blocked; смена gateway для того же дефекта не обходит его. Структурный исторический отчёт сам по себе не разрешает новый переход или завершение.

## Результат аналитика

`analysis_ready` использует массивы объектов, а не строки. ID уникальны внутри каждого массива. Поля:

- `requirements`: `requirement_id`, `description`, `status`, `source`.
- `risk_scenarios`: `scenario_id`, `requirement_id`, `risk`, `priority` (`high|medium|low`), `priority_reason`, `distinction`, `expectation_status`, `expected_result`, `expected_source`.
- `blocking_questions`: `question_id`, `question`, `requirement_ids`, `scenario_ids`, `blocks` (`planning|execution`). Затронутые сценарии обязательны.
- `assumptions`: `assumption_id`, `description`, `requirement_ids`, `scenario_ids`.
- `coverage_gaps`: `gap_id`, `description`, `requirement_ids`, `scenario_ids`.
- `test_plan_required`: boolean; `test_plan_reason`: непустая строка с обоснованием рекомендации координатору.

`status` и `expectation_status`: `confirmed` — подтверждено источником, `inferred` — вывод из контекста, `unknown` — неизвестно. Для `confirmed` и `inferred` источник — непустая строка с точной ссылкой; для `unknown` источник — `null`, а `expected_result` неизвестного сценария — также `null`. Описание неизвестного требования называет конкретную неопределённость. Остальные текстовые поля непусты.

Каждый сценарий ссылается на существующее требование. Подтверждённый сценарий требует подтверждённого требования. Каждый `inferred` или `unknown` сценарий связан с вопросом `blocks: planning`; до ответа его ожидание не является основанием для готового кейса. Вопрос `execution` может касаться подтверждённого сценария с недоступным условием выполнения. Массивы связей содержат уникальные непустые строковые ID существующих требований/сценариев; у вопроса, предположения или пробела хотя бы одна связь обязательна. Частичная неопределённость не блокирует независимые подтверждённые сценарии; отсутствие источников для надёжного анализа целиком означает `blocked`.

Валидатор проверяет структуру и связи. Координатор отдельно читает источники и оценивает корректность ожиданий, полноту покрытия и основания приоритетов. Подтверждённый пример: `examples/analysis/result.json`; пример с неизвестной частью: `examples/analysis/result-partial.json`. Старые строковые массивы нужно преобразовать в эту структуру.

## Структура результатов тест-плана

Для разделённой задачи применяется TEST_PLAN_PARTITION.md. Назначение содержит
необязательную привязку plan_partition к снимку и группе; события/каталог сохраняют
schema_version: 1. Групповой plan_ready относится только к назначенному набору.
Bound-автор возвращает base_revision из снимка; итоговый владелец также возвращает
partition_artifact с проверяемой сборкой. Scope проверяется при submit/review,
только schema-проверка сообщения не подтверждает объединённое покрытие.

Для `coverage_ready`, `plan_partial` и `plan_ready` обязательна непустая `plan_revision`. `plan_artifact` — локальный непустой файл; в новом отчёте все локальные пути абсолютны, относительные пути старых отчётов поддерживаются только для совместимости. `requirement_coverage` — непустой массив объектов:

```json
{"requirement_id":"R-1","source":"/absolute/path/qa/requirements.md#search","scenario_ids":["TC-001"],"priority":"high","verification":"UI","evidence":"Скриншот результатов","status":"ready"}
```

Для `blocked` и `excluded` обязателен `reason`; допустим пустой список ID вместо фиктивного кейса. `planned` и `ready` требуют ID сценариев. В каталоге находятся ровно кейсы из строк `ready`.

`open_questions` — массив объектов (может быть пустым):

```json
{"question_id":"Q-1","question":"Какие роли могут удалять?","requirement_ids":["R-2"],"scenario_ids":[],"blocks":"planning","status":"open"}
```

`blocks`: `planning`, `execution`, `none`; `status`: `open`, `resolved`. Для решённого вопроса обязательны `resolution` и `decision_source`. Все ссылки указывают на существующие ID в карте. Блокер планирования требует строки `blocked`, а каждый такой пробел — открытого вопроса. Готовое покрытие не может зависеть от открытого вопроса планирования. Правила маршрута и содержательного принятия — в `docs/TEST_PLAN.md`. Старые строковые карты нужно преобразовать в эту структуру.

## Условия переходов

После принятия plan_ready формируется производный HTML-план по TEST_PLAN_HTML.md.
Связи кейсов и требований берутся из requirement_coverage; обязательные поля событий
и каталогов сохраняются. Состояние presentation хранится в реестре отдельно от
авторских artifact_hashes. accepted означает содержательное принятие, ready HTML
проверяется отдельно; ошибка генерации не объявляется успешным представлением.

1. Исполнитель получает сценарий только после выполнения его обязательных условий стенда и данных. Частичная готовность не блокирует независимые сценарии.
2. `analysis_ready` требует связи существенных требований и рисков со сценариями и источниками ожидаемого поведения; вопросы, меняющие результат проверки, отмечаются как блокирующие.
3. `coverage_ready` передаёт раннюю карту и вопросы без объявления кейсов готовыми. `plan_partial` требует готовых и заблокированных частей без недетализированных строк `planned`; не завершает весь план. `plan_ready` не допускает незавершённого проектирования и требует связи существенных критериев приёмки с существующими ID кейсов из каталога либо обоснованного исключения. Подробные шаги и ожидаемые результаты находятся в каталоге, а не повторяются в плане и сообщении.
4. `environment_ready` требует подтверждённого выполнения всех пунктов переданного админгайда по [ENVIRONMENT_READINESS.md](ENVIRONMENT_READINESS.md): coordinator environment_assignment, environment_assignment_sha256 и guide_checks каждого пункта с действиями, статусом, временем и доказательствами. Непроверенный/невыполненный пункт исключает полную готовность. `data_ready` требует проверки свойств и связей данных.
5. `feature_test_complete` перечисляет каждый назначенный сценарий как выполненный, заблокированный или не запущенный; непроверенное не считается успешным.
6. `ready_for_review` требует конкретной версии изменений и отчёта о запуске либо причины отсутствия запуска.
7. `review_passed` относится к проверенной версии изменений. Значимые последующие изменения требуют нового ревью.
8. Автоматизация завершена, когда обязательный запуск и ревью выполнены; исключения должны быть явно отражены в результате.
9. Jira-дефект оформляется при достаточных доказательствах нарушения требования. Повторное воспроизведение — один из возможных видов доказательств; после записи задача проверяется повторным чтением.

Если два последовательных цикла исправлений не дают новой информации или не закрывают замечания, координатор фиксирует блокер вместо бесконечного повторения.

## Дополнительные материалы качества

Общее поле верхнего уровня `self_review` описано в SELF_REVIEW.md: schema_version=1, checks для scope/evidence/consistency/role со status, note и evidence, массивы fixes/remaining_risks. Для новых назначений реестра и запусков runner обязательно, старые отчёты совместимы. Самопроверка привязана к окончательным байтам отчёта и не заменяет содержательное принятие.

Для отчётности используются отдельные scope/runs/review-артефакты по `docs/TEST_REPORTING.md`. Старые feature_test_complete и blocked остаются совместимыми. Роль qa-test-report-writer возвращает test_report_ready; валидатор проверяет связанные файлы, исходные хеши, вычисленные факты, task_id, ревизию и точный список ограничений. Готовый документ может содержать непроверенные бизнес-кейсы; он не означает завершение тестирования или решение о выпуске. Принятие содержания — за координатором.

Формат рабочих событий и обычный каталог кейсов сохраняются. Модель поведения, проектные строки комбинаций и ревью контрпримеров передаются ссылками в существующем `artifacts`, правила — в TEST_DESIGN.md. Готовое покрытие комбинаций использует необязательное расширение payload.combination_coverage; каталог добавляет combination_values или parameter_sets, строки — scenario_bindings. Критерий kind=combination_coverage требует полного независимого combination_checks; хеш согласованной модели и вложенных материалов проверяется кодом. Профиль исходного состояния, добавленное покрытие, discovery и контролируемые ошибки автотеста — в артефакте по AUTOTEST_QUALITY.md; `run_result`, `checks` и `limitations` ссылаются на выполненные проверки или явно сохраняют ограничения. Названия автотестов и Allure-шагов проверяются по AUTOTEST_NAMING.md; связь с scenario_id и доказательство свежего отображения либо ограничение сохраняются в том же артефакте качества и существующих checks/limitations. Состав набора и подготовка данных проверяются по TEST_DATA.md; таблица назначения записей, рецепт, фактические свойства и очистка передаются существующими artifacts/verified_properties/cleanup и материалами ревью. Рецепт без проверки предусловий не подтверждает data_ready.

Математическое покрытие, наличие файла, discovery и успешный запуск подтверждают разные свойства. Не заменяй содержательную оценку источников одним из этих результатов. Если обязательная по назначению проверка недоступна, этап не считается полностью принятым.

## Состояние отдельного запуска

`scripts/run_test_automation.py` сохраняет статус каждого назначения рядом с файлом задания в `.qa-agent-state/`; ключ состояния зависит от `task_id` и репозитория, а не от пути результата. Запись создаётся перед вызовом CLI и обновляется после него. Повторный запуск с тем же `request_id` отклоняется даже после ошибки или превышения лимита времени: сначала проверь внешнее состояние, затем создай новое назначение с новым `request_id`. При использовании `--state` передавай один и тот же путь для всей задачи. `reported` означает валидный отчёт исполнителя, а не завершение всей автоматизации; `ready_for_review` дополнительно получает `review_status=pending`.

## Журнал сбоев процесса

Журнал FAILURE_JOURNAL.md отделён от рабочих QA-событий: их обязательные поля не меняются. Координатор связывает incident_id с task_id/request_id, назначением и доказательствами. Ошибка процедуры или инструмента, найденный дефект продукта и корректный запрос недостающего условия различаются. Runner передаёт failure_incident_id через метрики; координатор обновляет этот инцидент вместо повторной регистрации. Структурный успех записи не подтверждает смысл причины или восстановление.

## Готовность заглушек

qa-stub-builder сначала может вернуть stubs_plan_ready без мутаций. Координатор оценивает намерение и фиксирует назначение v2; исполнитель не меняет его определения/ожидания/разрешения. configure_only возвращает stubs_configured: configured_scenarios плюс blocked_scenarios ровно покрывают назначение, execution-ready scenario_ids не заявляются. Для verify stubs_ready требует готовности всех ID; stubs_partial — непустые готовую и заблокированную части без пересечения. Непроверенная зависимость блокирует все связанные сценарии. Полная блокировка — blocked с попытками и уже созданными ID. CLI требует внешний --stub-assignment и весь --scenario-id; для черновика внешний файл ещё не нужен. Только новые артефакты заглушек имеют schema_version=2, прочие схемы сохраняются. Готовность заглушек не подтверждает продукт или реальную интеграцию.

## Связанное содержательное ревью в текущих исходниках

`qa-result-reviewer` возвращает `review_passed`, `changes_requested` или `blocked`.
Новый `review_target` фиксирует автора, хеш результата/артефактов, scope и
`review_criteria_ids`. Payload содержит target_request_id, target_sha256,
review_scope, typed checks, findings, limitations; подробный формат и ограничения —
[CONTENT_REVIEW.md](CONTENT_REVIEW.md). `qa-autotest-reviewer` с таким назначением
сохраняет также reviewed_revision. Старые отчёты без review_target совместимы.
Ревью по форме не доказывает смысл; неизвестное не может стать review_passed.

`qa-conclusion-reviewer` использует тот же связанный протокол и обязательные conclusion_checks: criterion_id, pointer, точное значение conclusion, status, note, source_refs с path/quote. Required_reviews[].conclusions фиксирует проверяемые JSON-поля, включая /summary. Назначение содержит conclusion_paths и первичные review_sources с review_source_hashes; поля/хеши/цитаты и независимость проверяются при submit/assess/finish. Формат и границы — [CONCLUSION_REVIEW.md](CONCLUSION_REVIEW.md).

Опциональный review_discussion фиксирует хеш packet, round и prior_request_id своего направления. Discussion_checks содержит каждый issue_id, maintain/revise, resolved/unresolved, note и первичные source_refs либо свежий собственный receipt. Submit сверяет изменение статуса с прошлым отзывом и доказательствами; приёмка не смешивает первоначальные оценки и разные раунды. Лимит 1..2, первичные blocking-замечания сохраняются — [REVIEW_DISCUSSION.md](REVIEW_DISCUSSION.md).

При required_reviews[].execution payload содержит execution_checks: criterion_id,
check_id, status, note и локальный receipt нового scoped вызова. Supervisor выделяет
binding review_execution конкретной попытки; шлюз сохраняет OBS и реальные аргументы/
ответ, реестр проверяет свежесть, владельца, target и хеши. Неизвестный/сорванный
запуск остаётся blocked; падение assert внутри успешного вызова отличают от ошибки
инструмента. Подробности — [REVIEW_EXECUTION.md](REVIEW_EXECUTION.md).

## Командная доказательность benchmark

Назначение с evidence_session_id требует payload.benchmark_summary_artifact. Валидатор получает независимый --evidence-session-id, пересчитывает сводку и требует summary точно из summary_text. При незакрытом benchmark-этапе допустим blocked, а не готовое событие. Реестр включает сводку и все source_hashes в защищённые артефакты, повторно проверяя их до handoff/приёмки. Полный маршрут — BENCHMARK_EVIDENCE_GATES.md. Уведомление, таблица вне grading-пакета, неподтверждённое число или неизвестная схема не являются принятым результатом.
