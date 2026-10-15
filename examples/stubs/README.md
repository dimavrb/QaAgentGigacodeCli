# Учебная передача REST/Kafka-заглушек

Все файлы синтетические: MCP не вызывался. definition иллюстрирует непрозрачный провайдерский объект и **не является форматом sbermock**. Реальный формат и tools агент читает в подключённом приложении.

assignment.json — независимое намерение координатора v2: цель, режим verify, разрешённые операции и определения/input/expected. profile.json содержит две заглушки для TC-REST и TC-KAFKA, их фактические ID, времена и очистку. verification.json связан с назначением и профилем; в observations/ отдельные readback/положительный/отрицательный контроль. result.json передаёт готовую часть. Источник — requirements.md; JSON-артефакты заглушек имеют schema_version=2.

```bash
python3 scripts/validate_qa_result.py \
  --input examples/stubs/result.json \
  --task-id QA-STUB-DEMO --request-id QA-STUB-DEMO-1 \
  --agent qa-stub-builder --scenario-id TC-REST --scenario-id TC-KAFKA \
  --stub-assignment examples/stubs/assignment.json
```

Ожидается код 0. Без полного --scenario-id или внешнего --stub-assignment результат отклоняется. Самовольное изменение определения/expected, лишнее вложенное условие matcher, старое наблюдение, другой project/environment, отсутствие отрицательного контроля или unverified-зависимость не принимаются как stubs_ready.

Перед исполнением назначение проверяется без MCP через scripts/validate_stub_assignment.py с --input assignment.json, task/request и полным --scenario-id. Структурный успех не заменяет оценку контракта/прав координатором.

Для нового назначения через manage_task.py передайте этот scope отдельным
полем stub_assignment рабочего задания, а scenario_ids, task_id и request_id
рабочего задания согласуйте с ним. Реестр автоматически заморозит scope до
prepare и включит все реально проверенные вложенные наблюдения в artifact_hashes.
Их можно использовать в assess как первичные доказательства. Старые материалы
этого примера и их хеши не обновляются для имитации свежего запуска.

Если результат лежит отдельно от доказательств, задайте artifact_base в рабочем
назначении или --stub-evidence-base в standalone CLI. Времена из будущего не
принимаются. Для ревью профиля/проверок укажите review_target.scope=stubs и передайте
первичные файлы reviewer через inputs; сам по себе стандарт не заменяет их чтение.

Черновик без мутаций: повтори команду с --input examples/stubs/plan-result.json без --stub-assignment. Успех означает проверенный формат черновика, а не право выполнять изменения. В реальной работе координатор выдаёт отдельное назначение после оценки.

Для настройки без проб используй отдельную синтетическую сцену:

```bash
python3 scripts/validate_qa_result.py \
  --input examples/stubs/configure-only/result.json \
  --task-id QA-STUB-CONFIG --request-id QA-STUB-CONFIG-1 --agent qa-stub-builder \
  --stub-assignment examples/stubs/configure-only/assignment.json \
  --scenario-id TC-REST --scenario-id TC-KAFKA
```

Ожидается stubs_configured: обе конфигурации прочитаны, публикаций/проб нет, выполнение продукта не подтверждено. Это независимые учебные сцены, не последовательные реальные операции в одном sbermock.

Для partial укажи status=blocked и reason, перенеси **все** зависимые ID в blocked_scenarios. Для настроенной записи без runtime проверка имеет status=configured/readback/reason; зависимости не становятся execution-ready. Pending использует operation=pending, явный null resolved_at и cleanup.action=inspect; неизвестный результат записи сначала требует чтения состояния.

После использования созданных правил очищаются только сохранённые ID. Для reused очистка none; для updated — restore с previous_definition и hash-проверенным before-снимком назначенного ID. Это план, не выполненная очистка. Kafka-пробы дополнительно связываются correlation/window/position. Реальная проверка владения/прав, конфликтов, Kafka-схем и MCP-подключения остаётся работой по STUBS.md.
