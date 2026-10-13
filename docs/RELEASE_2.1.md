# Поставка QA-агента 2.1

Дата: **2026-10-12**. Архив: `qa-agent-2.1.zip`, checksum:
`qa-agent-2.1.zip.sha256`. PACKAGE_MANIFEST.json содержит версию, дату, размеры
и SHA-256 файлов. Поставка: **14 специализированных ролей**, **5 skills**, `/qa`.
Исторический архив 2.0 сохранён для отката.

## Изменения относительно 2.0

- qa-impact-analyst: выбор тестов по снимку всей поставки и косвенным зависимостям.
  Обязательные/новые/ранее падавшие/неизвестные тесты сохраняются; неполнота расширяет
  регресс. Артефакт пересчитывается по независимому impact_input, ready-передача
  ограничена выбранными ID. Исключения требуют независимого reviewer.
- Доказательность benchmark: дословные цитаты response, замороженный пакет копий,
  изолированная оценка, приёмка файла по абсолютному пути, проверка схемы/полноты.
  Числа и сводки пересчитываются; пустой ответ — failed с сохранением usage.
  Сравнение ограничено измеренным разбросом; письменность проверяется механически.
- Контроль смысла оценок: слепые пары правильных/ошибочных ответов, общий harness
  штатного grader, ошибки одобрения/блокировки и отложенная выборка. Предварительная
  учебная эталонная разметка требует экспертного ревью до квалификации оценщика.
- Импорт нормализованных наблюдений покрытия с привязкой к сборке/каталогу/стенду/
  данным. Устаревшие/неполные связи расширяют прогон; повтор не стирает провал.
- Пилот двух фактических исполнений full/selected: пропущенные подтверждённые дефекты,
  инфраструктура/нестабильность и полное время до приёмки. Один пилот не разрешает
  автоматическое внедрение и не гарантирует будущего покрытия.
- Сквозные метрики, импорт первичных worker metrics, объединение перекрытий,
  сохранение неизвестного usage; план по длительностям/workers/resources и локальное
  исполнение с блокировками и сохранением всех попыток.
- improvement_plan закрепляет независимые входы и явные пороги в реестре/валидаторе;
  отчёты и summary пересчитываются, нарушение порогов блокирует готовность.

Прежние managed/review/context механизмы сохраняются. Skills-кандидаты qa-test-data/
qa-stub-contract остаются для явного пилота, без включения по умолчанию.

## Установка и совместимость

Первая установка: INSTALL.md, IMPORT_INTO_APP.md, SETUP_AND_UPDATE.md.
Переход с 2.0: UPGRADE_2.1.md и UPDATE.md. Обновляйте весь пакет и подключённые
копии ролей/skills/команды. Python 3.10+, POSIX: macOS/Linux, Windows через WSL.
Playwright MCP pin **0.0.83** сохраняется. Локальные модель/MCP/секреты сохраняйте.

Схемы сообщений/каталога не повышаются массово; новые протоколы задаются назначениями.
Исторические отчёты/execution-состояния не переписываются. Старые ведомости не
становятся новыми пакетными оценками: legacy-проверка цитат — только аудит.
Изменение инструкций/сборщика требует нового плана и сверки внешних действий.
Runtime, ответы, browser_pool, gateway-журналы и доказательства храните вне пакета.

## Проверки поставки

Из корня распакованной копии:

```sh
python3 scripts/package_agent.py --verify /absolute/path/qa-agent-2.1.zip
python3 -m unittest discover -s tests -v
python3 scripts/validate_test_cases.py --input examples/test-cases.json
python3 -m unittest discover -s examples/test-data -v
python3 examples/impact-analysis/build_demo.py --output-dir /tmp/qa-agent-2.1-impact
python3 scripts/validate_qa_result.py --input /tmp/qa-agent-2.1-impact/result.json --task-id QA-IMPACT --request-id QA-IMPACT-1 --agent qa-impact-analyst --impact-input /tmp/qa-agent-2.1-impact/impact-input.json --require-self-review --self-review-assignment /tmp/qa-agent-2.1-impact/assignment.json
python3 examples/qa-improvements/build_demo.py --output-dir /tmp/qa-agent-2.1-pilot
python3 scripts/qa_improvements.py verify --report /tmp/qa-agent-2.1-pilot/shadow.json
python3 scripts/qa_improvements.py verify --report /tmp/qa-agent-2.1-pilot/schedule.json
```

Ожидаются **574 теста пакета** и один отдельный учебный тест данных. Для повторов
используйте новые каталоги. Проверяются собственные локальные процессы, блокировки,
отмена/timeout и stdio MCP. Тесты не вызывают живую модель или продукт.

Рядом с ZIP: RELEASE_2.1.md, UPGRADE_2.1.md, INSTALL_2.1.md, UPDATE_2.1.md,
IMPORT_INTO_APP_2.1.md, SETUP_AND_UPDATE_2.1.md и VALIDATION_2.1.json. Протокол
проверяет конкретный ZIP: checksum/манифест, совпадение с исходниками, распакованную
копию, воспроизводимость и CLI. В ZIP входят .qwen/docs/scripts/tests/examples и
корневые инструкции. Artifacts/dist/runtime/git/IDE/cache не входят.

## Пределы проверки

Локальная приёмка подтверждает механику. Живые Qwen, Playwright/sbermock/Jira,
продуктовый CI/Allure и ускорение регресса не измерены. Адаптер реального коллектора
покрытия нужен отдельно. Контрольный эталон предварительный. Живой baseline и
сквозной пилот ROADMAP_3.0.md остаются незавершёнными; номер 2.1 не закрывает их.
