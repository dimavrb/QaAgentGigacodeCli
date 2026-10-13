# Поставка QA-агента 1.5

Дата: 2026-10-05. Поставка: `qa-agent-1.5.zip` и `qa-agent-1.5.zip.sha256`. Состав и хеши файлов — в `qa-agent/PACKAGE_MANIFEST.json` внутри ZIP. Python-скрипты используют только стандартную библиотеку.

## Что изменилось

- Добавлена роль `qa-test-report-writer`; всего специализированных ролей — 10. Она составляет отчёт из сохранённых материалов, без новых действий в продукте.
- Основная часть отчёта показывает бизнес-цель, планируемое действие, правило, ожидание, записанные шаги, наблюдение и границы вывода. Одинаковые описания не повторяются для каждого варианта. ID и ссылки расположены после сути.
- Бизнес-кейсы связаны со сценариями и конфигурациями через scope. Условия и фактические значения тогглов сохраняются исполнителем в момент прогона; неизвестные и fallback не подтверждают целевой вариант.
- `scripts/build_test_report.py` создаёт `report.json` и `report.md`, проверяет источники, счётчики и историю. Частичный повтор заменяет только пересечение сценариев; без явного supersedes конфликт не скрывается выбором успешного запуска.
- Ревью связано с хешами первичного результата, метаданных, источников и доказательств. `test_report_ready` проверяется новым валидатором и означает готовый документ, включая неполное тестирование.
- Аналитик, автор плана, исполнитель, роли окружения и данных передают необходимые сведения для отчётности. Подробный и базовый учебные примеры находятся в `examples/test-reporting/`.

## Совместимость и установка

Прежние QA-события версии 1.4, тест-план и каталог `schema_version: 1` сохраняются. Новые scope/runs/review отделены от прежней матрицы исполнения. `business_goal` и `intent` необязательны; недостаток подробностей показывается явно. Старые принятые результаты не переписываются. `test_report_ready` требует валидатора 1.5; после отката 1.4 его не принимает.

Исторические снимки отчётности сохраняются вместе с их входами и версией сборщика: Markdown проверяется по правилам этой версии. Новый вид отчёта создаётся в новом каталоге с новой ревизией. Отсутствующие условия прошлого прогона не заполняются текущими значениями.

Краткая инструкция — `UPGRADE_1.5.md`, полное задание обновления — `UPDATE.md`, первая установка — `IMPORT_INTO_APP.md`, настройка — `SETUP_AND_UPDATE.md`. Рядом с ZIP поставляются их копии: `UPGRADE_1.5.md`, `UPDATE_1.5.md`, `INSTALL_1.5.md`, `SETUP_AND_UPDATE_1.5.md` и `RELEASE_1.5.md`. Результаты проверки конкретного ZIP — во внешнем `VALIDATION_1.5.json`.

В проекте приложения обновляются вложенный пакет и подключённые QA-роли, навыки, `/qa` и APP_QWEN.md. Локальные tools/MCP, APP_CONTEXT.md и сокращённый QWEN.md сохраняются. Права reporter на работу с артефактами не являются технической песочницей. Playwright MCP остаётся `0.0.83`. Архивы предыдущих версий не изменяются.

## Проверки поставки

Из корня исходников и затем из распакованной копии в новом каталоге:

```bash
python3 -m unittest discover -s tests
python3 scripts/validate_test_cases.py --input examples/test-cases.json
python3 scripts/validate_qa_result.py --input examples/analysis/result-partial.json --task-id QA-HANDOFF --request-id analysis-2 --agent qa-analyst
python3 scripts/validate_qa_result.py --input examples/handoffs/feature-blocked.json --task-id QA-HANDOFF --request-id feature-2 --agent qa-feature-tester --scenario-id TC-001 --scenario-id TC-002
```

Сборка двух вариантов учебного отчёта в новых каталогах:

```bash
QA_REPORT_CHECK_DIR=$(mktemp -d)
python3 scripts/build_test_report.py --scope examples/test-reporting/scope.json --runs examples/test-reporting/runs.json --revision check-basic --output-dir "$QA_REPORT_CHECK_DIR/basic"
python3 scripts/build_test_report.py --check "$QA_REPORT_CHECK_DIR/basic"
python3 scripts/build_test_report.py --scope examples/test-reporting/scope.json --runs examples/test-reporting/detailed-runs.json --revision check-detailed --output-dir "$QA_REPORT_CHECK_DIR/detailed"
python3 scripts/build_test_report.py --check "$QA_REPORT_CHECK_DIR/detailed"
```

Оба варианта: planned=3, target_executed=1, passed=1, accepted_passed=1. Оба бизнес-кейса incomplete: OFF не запускался, отмена заблокирована. Подробный отчёт содержит действия и наблюдение, базовый сохраняет краткую старую запись. Никакого выполнения реального продукта в этих примерах нет.

Внутренний манифест проверяется через `scripts/package_agent.py --verify <ZIP>`, внешний checksum — из каталога поставки:

```bash
shasum -a 256 -c qa-agent-1.5.zip.sha256
```

В ZIP не входят Git, IDE, кэши и рабочие артефакты, включая отчёты из `artifacts/test-reports/`. Проверка Python и синтетических данных не подтверждает доступ к модели, качество ответов живого Qwen или работу браузера/CI приложения; это проверяется после установки в соответствующем окружении.
