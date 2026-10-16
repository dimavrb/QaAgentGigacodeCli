# Проверки QA-пакета

Набор проверяет сам пакет, не приложение пользователя. Основная команда из корня:

```bash
python3 -m unittest discover -s tests -v
```

Python 3.10+, стандартная библиотека. Интеграционные проверки создают временные
Git-репозитории, дочерние процессы и loopback HTTP/MCP-серверы; им нужны локальные
права на эти действия. Сеть, Jira и модель для этого набора не требуются.
CI запускает тот же набор на Linux/Python 3.10 и macOS/Python 3.12, отдельно
examples/test-data, затем собирает и проверяет ZIP. Локальный результат одной
платформы не подтверждает обе платформы CI.

## Карта проверяемых механизмов

Имена ниже — модули test_*.py. Один механизм может иметь несколько уровней
проверки; это не основание удалять интеграционный тест как дубликат unit-теста.

| Механизм | Основные тестовые модули | Что проверяется |
| --- | --- | --- |
| Оболочка, роли, self-review | qa_contract, role_handoffs, self_review, stub_handoffs | Обязательные поля, события, blocking, ссылки на критерии, совместимость передач |
| Реестр и критерии задачи | task_exchange, task_acceptance, qa_tasks, task_create_cli | Ревизии, хеши, владельцы, зависимости, scope и запрет ложного завершения |
| Передача контекста | context_assembly, coordinator_context, case_context, test_data_context | Снимки, fencing, выбор разделов, валидность примеров, scoped-профили |
| Managed/runtime/процессы | managed_execution, agent_runtime, process_control, runner | Запуск, timeout, отмена, повтор, checkpoint, идентичность процесса, неизвестный usage |
| Gateway и браузер | action_gateway, browser_pool | Ограничения tools, idempotency, uncertain write/readback, lease и карантин |
| Ревью и принятие | check_quality, review_execution, review_discussion, conclusion_review | Независимость, текущая ревизия, реальные локальные probes, возврат и бюджет |
| Кейсы и публикация | test_cases, api_case_details, manual_allure, test_plan | JSON-каталог, HTTP-детали, представление Allure, согласованность карты |
| Комбинации и части плана | combinations, combination_coverage, plan_partition | Ограничения сочетаний, покрытие взаимодействий, владение и слияние частей |
| Готовность и заглушки | environment_readiness, data_readiness, stub_contract, test_data_example | Полнота назначенного набора, partial/blocked, первичные снимки, lifecycle |
| Анкеты | questionnaire, answer_wait | HTML против serve, POST, атомарность, deferred/comment, ожидание и восстановление |
| Расследование/Jira/журнал | investigation_state, defect_verification, jira_publication, jira_defect_skill, failure_journal | Свежесть и происхождение доказательств, факт/причина, публикация и ограничения |
| Отчёты и HTML | test_reporting, plan_html, environment_html | Бизнес-кейсы × конфигурации, неполные статусы, источники и ссылки |
| Влияние и улучшения | impact_analysis, qa_improvements | Fallback при неполноте, обязательный scope, измеренные данные и приёмка |
| Установка и поставка | package_agent, extension_resources, extension_project, project_install, project_hardening, project_agents, agent_role_updates, gigacode_configuration | Состав, native-копии, сохранение адаптаций, целостность и portable-установка |
| Обновление и восстановление | auto_update, project_update, update_hardening | Локальный Git, version/CI gates, прерывание, rollback, сохранение проекта |
| Eval-механика | benchmark_execution, benchmark_suite, benchmark_review, benchmark_evidence, profile_skills, agent_trace | Capture без self-grade, изоляция рубрики, полные ведомости/повторы, хеши, сводки и сопоставимость |

Исторические test_first_package.py/test_second_package.py разделены на
**task_acceptance, benchmark_execution, agent_trace, managed_execution,
action_gateway, process_control**. Все 70 методов и их assertions сохранены.
Общие фикстуры находятся в *_fixtures.py; они не содержат обнаруживаемых тестов.
Тесты обновлений используют UpdateFixture, а не setUp другого тестового класса.
Исторические документы выпуска с прежними командами остаются историческими.

## Как проверять изменение

Сначала запускай соответствующие модули, например:

```bash
PYTHONPATH=tests python3 -m unittest test_benchmark_suite test_benchmark_execution test_questionnaire -v
```

После изменений выполни полный discover по AGENTS.md. Проверку CLI дополняй
наблюдаемым эффектом/отказом, не только --help. Для рефактора сохраняй прежние
поведенческие assertions и сверяй discovery: потеря или двойной запуск тестов —
регресс. Новые проверки должны обнаруживать конкретную ошибку: например, запись
в обход readback, изменённый prompt или ложное закрытие ещё не оценённого eval.
Проверка наличия фразы в Markdown не доказывает, что модель соблюдает правило.

## Eval и границы полноты

[Регрессионный набор](../examples/benchmark/REGRESSION.md) содержит 17 задач и
68 критериев. Старые suite (5/20), quality-suite (9/36), profile-suite (15/60)
сохранены. В объединении наборов каждая из 14 ролей имеет минимум одно reasoning-
задание; координатор имеет отдельные случаи. Это минимальная представленность,
не полное покрытие поведения роли. Из-за повторов старых задач размеры наборов
нельзя складывать как число уникальных заданий.

Python-тесты проверяют механику этих наборов на программном CLI, где
measurement_kind=fixture и оценки not_checked. Смысл ответа оценивается отдельно
по [EVALUATION.md](../docs/EVALUATION.md) и гейтам доказательности.
В среде этого аудита GIGACODE не установлен: живое качество, стоимость,
интерактивные guard/MCP и фактический 429 не измерены. Протоколы из другого
проекта недоступны и не подменяются учебными заданиями.

Процент строк/ветвей здесь не измерен: coverage.py не установлен. Карта выше,
статические ссылки и зелёный discover не удостоверяют все ветви. Не закрыты
реальные Jira/Kafka/Postgres/браузер и глобальный limiter/checkpoints слоя B
COORDINATION_SAFETY.md. Полнота дальнейшего набора оценивается по новым рискам,
известным дефектам и независимым контрпримерам, а не только числу тестов.

Статическая инвентаризация 91 Python-модуля нашла две CLI-оболочки без прямого
тестового вызова: scripts/manage_review_cycle.py и
scripts/measure_benchmark_dispersion.py. Их ядро review_cycle/calibration проверено
в check_quality и benchmark_evidence; разбор аргументов/вывод самих этих оболочек
ещё не подтверждён самостоятельным CLI-тестом. Это отдельный узкий пробел,
а не утверждение об отсутствии тестов базового механизма.
