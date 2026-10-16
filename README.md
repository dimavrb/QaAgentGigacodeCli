# QA Agent — GIGACODE CLI · 2.3.13

Переносимый QA-пакет: координатор, 14 специализированных ролей, 6 skills,
проверяемые контракты результатов и Python-инструменты. Основной профиль —
**GIGACODE CLI от GitVerse**. Общая QA-логика отделена от среды исполнения;
для другого CLI нужны совместимый адаптер и его собственный профиль ролей.

## Быстрый старт

Основной способ — [проектная установка](docs/PROJECT_INSTALLATION.md): обычные
файлы в `.qa-agent/package/`, роли/skills/команды в `.gigacode/`. **QA MCP-сервер
не требуется и не поставляется.** Нужны Python 3.10+, настроенный GIGACODE,
macOS/Linux или WSL. Python-зависимости — стандартная библиотека.

Получите проверенный выпуск в отдельную папку, затем из обычного терминала:

```bash
python3 /path/to/qa-agent/scripts/qa_install.py --project-root /path/to/app
python3 /path/to/qa-agent/scripts/qa_install.py --project-root /path/to/app --apply
```

Первая команда показывает план, вторая устанавливает либо обновляет весь комплект.
При конфликте прежние файлы сохраняются. Пакет, профиль и `.qa-agent/install.json`
можно хранить в Git одним коммитом; абсолютных путей компьютера в manifest нет.

Из корня приложения:

```bash
python3 .qa-agent/package/scripts/qa_project.py init --project-root .
python3 .qa-agent/package/scripts/qa_project.py doctor --project-root .
python3 .qa-agent/package/scripts/qa_start.py --project-root .
```

В новой сессии проверьте `/qa:doctor`, `/agents manage`, `/skills`, `/help`,
`/memory`, затем выполните `/qa <цель>`. QA-команды запускают скрипты внутри проекта.
Обновления выполняются между сессиями тем же установщиком из нового выпуска;
qa-start в проектном режиме только проверяет комплект и запускает GIGACODE.
Переход с глобального extension — [UPGRADE_2.3.13.md](docs/UPGRADE_2.3.13.md).

Начните с задачи по требованиям, без внешних систем. Для реальных проверок
укажите стенд, сборку, данные, разрешённые действия и источники ожиданий.
Модель, авторизация, Jira/sbermock и другие MCP-подключения настраиваются отдельно
в вашей установке; секреты в Git не добавляются. Автоматического опроса Jira нет.

Для оформления дефектов включён универсальный
[jira-defect-creator](.gigacode/skills/jira-defect-creator/SKILL.md): поиск дублей,
сводный чек-лист, проверка аргументов и сохранённой задачи. При первом использовании
настройте профиль целевого проекта; идентификаторы и опции из других проектов
не переносятся. Профиль, черновики и журналы храните вне поставки.

## Как устроен пакет

| Компонент | Назначение |
| --- | --- |
| `docs/COORDINATOR.md` | Общие правила QA-координатора |
| `GIGACODE.md` | Короткая точка входа GIGACODE с импортом координатора |
| `AGENTS.md` | Общие правила работы с репозиторием |
| `agents/`, `skills/`, `commands/` | Корневые ресурсы для native discovery расширения: 14 ролей, 6 навыков и 5 команд |
| `.gigacode/agents/` | 14 ролей: анализ, план, импакт-анализ, стенд, данные, заглушки, тестирование, расследование, Jira, автотесты, три направления ревью и отчёт |
| `.gigacode/skills/` | Кейсы, браузер, качество проверок, универсальный Jira-навык и два профильных кандидата для пилота |
| `.gigacode/commands/qa.md` | Команда `/qa` |
| `.gigacode/settings.json` | Переносимый Playwright MCP-профиль без credentials |
| `qa-runtime.json` | Исполнитель по умолчанию и контракт headless-запуска |
| `gigacode-extension.json`, `GIGACODE_EXTENSION.md` | Манифест и контекст расширения |
| `scripts/qa_install.py`, `scripts/qa_project.py` | Проектная установка/обновление, переносимые метаданные, контекст и диагностика |
| `scripts/qa_tasks.py`, `docs/TASK_STORAGE.md` | Список/чтение задач, единые пути и восстановление по реестру |
| `scripts/qa_release.py`, `scripts/qa_recover.py`, `docs/UPDATE_SAFETY.md` | Проверка выпуска и независимый rollback вне checkout |
| `scripts/`, `tests/`, `examples/` | Валидаторы, управляемое исполнение, измерения и учебные сценарии |

Для расширения GIGACODE читает каталоги `agents/`, `skills/`, `commands/` в корне
пакета. `.gigacode/…` сохранены как исходники для проектного режима и совместимости
обновлений. После правки этих исходников выполните
`python3 scripts/extension_resources.py --apply`; сборка и preflight отклоняют
отсутствующие или отличающиеся корневые копии. Подробности — [EXTENSIONS.md](docs/EXTENSIONS.md).

Координатор выбирает нужные этапы, регистрирует назначения и проверяет первичные
доказательства. Самопроверка автора и независимое ревью предшествуют приёмке
критериев всей задачи. Подтверждённый дефект продукта может быть корректным итогом
QA-работы. Неизвестное и обязательные блокеры не превращаются в успешный результат.

## Проверки и headless-запуск

```bash
python3 -m unittest discover -s tests -v
python3 scripts/agent_runtime.py --mode automation
python3 scripts/agent_runtime.py --mode gateway
```

`agent_runtime.py` проверяет CLI без обращения к модели. Headless-режимы требуют
определённых флагов; несовместимый CLI останавливается до задания. Поддержка формата
проектных файлов GIGACODE не означает поддержку всех флагов вашей версией CLI.
Для реального допуска нужно проверить ограничения инструментов на своей сборке.

```bash
python3 scripts/run_test_automation.py \
  --repo /absolute/path/to/autotests \
  --task-file /absolute/path/to/task.md \
  --output /absolute/path/to/result.json
```

Исполнитель выбирается через `qa-runtime.json`, `--cli` и `--runtime-config`.
Задачу, состояние и доказательства храните вне заменяемых файлов пакета.
Для автотестов используйте отдельную рабочую копию целевого репозитория.

## Документация

- [Установка](docs/INSTALL.md), [перенос в приложение](docs/IMPORT_INTO_APP.md), [обновление](docs/SETUP_AND_UPDATE.md).
- [Распространение как extension](docs/EXTENSIONS.md), [пути и состояние](docs/EXTENSION_RUNTIME.md).
- [Автообновление при запуске QA-сессии](docs/AUTO_UPDATE.md).
- [Среды исполнения и переносимость](docs/RUNTIMES.md), [миграция на GIGACODE](docs/UPGRADE_2.1.2.md).
- [Маршруты](docs/WORKFLOWS.md), [контракт](docs/CONTRACT.md), [приёмка](docs/TASK_ACCEPTANCE.md).
- [Управляемое исполнение](docs/MANAGED_EXECUTION.md), [MCP-шлюз](docs/ACTION_GATEWAY.md), [сборка контекста](docs/CONTEXT_ASSEMBLY.md).
- [Качество и ревью](docs/CONTENT_REVIEW.md), [ревью выводов](docs/CONCLUSION_REVIEW.md), [ревью с исполнением](docs/REVIEW_EXECUTION.md).
- [Импакт-анализ](docs/IMPACT_ANALYSIS.md), [измерения](docs/QA_IMPROVEMENTS.md), [benchmark](examples/benchmark/README.md).
- [Тестовые данные](docs/TEST_DATA.md), [заглушки](docs/STUBS.md), [браузерный пул](docs/BROWSER_PROFILES.md), [HTML-вопросы](docs/QUESTIONS_UI.md).
- [Проверенная готовность данных](docs/DATA_READINESS.md): свойства, связи, видимость и передача доступного набора; [учебный протокол](examples/data-readiness/README.md).
- [Подтверждённая публикация Jira](docs/JIRA_PUBLICATION.md): принятый дефект, дубли, одна операция создания и чтение сохранённых полей; [синтетический пример](examples/jira-publication/README.md).
- [Ручные кейсы: JSON → вебхук GitVerse → Jenkins → ТестОпс](examples/testops-jenkins/README.md).
- [Отбор ручных сценариев](docs/TEST_CASE_SELECTION.md), [понятные шаги](docs/TEST_CASE_STYLE.md), [тестовая задача навыка](docs/TEST_CASE_PILOT.md).
- Кейсы «Шаг — Ожидаемый результат — Тест-дата»: [API](examples/test-cases/api/INDEX.md), [UI](examples/test-cases/ui/INDEX.md) и [Kafka](examples/test-cases/kafka/INDEX.md).
- [Передача примеров агентам и проверка каталога](docs/TEST_CASE_REVIEW.md): роли, замороженный контекст и независимое ревью.
- [HTML-план после итогового ревью](docs/TEST_PLAN_HTML.md): поиск кейсов и переходы к покрываемым требованиям.
- [Несколько авторов тест-кейсов](docs/TEST_PLAN_PARTITION.md): границы групп, отдельные контексты и проверка полного объединения.
- [Поставка 2.3.13](docs/RELEASE_2.3.13.md), [история](CHANGELOG.md), [план развития](docs/ROADMAP_3.0.md).

## Сохранённые QA-задачи

Новый реестр создавайте локальным `scripts/qa_task_create.py --criteria <file>`.
Команда сохраняет существующую задачу при идентичном повторе; путь реестра стандартный.
Примеры — [TASK_STORAGE.md](docs/TASK_STORAGE.md).

`/qa:tasks` показывает незавершённые задания проекта, `/qa:resume QA-123`
восстанавливает выбранное по `.qa-agent-state/tasks/*/task.json`. Пустая `/qa`
предлагает выбрать задачу или задать новую цель. Локальный CLI читает состояние;
продолжение требует проверки владельца, доказательств и актуальных условий.
Планы и кейсы команды — в Git, локальные реестры и доказательства — вне поставки.
Единые пути и CLI fallback — [TASK_STORAGE.md](docs/TASK_STORAGE.md).

## Пределы проверки

Локальные тесты проверяют механику на фикстурах и собственных процессах. Качество
живого GIGACODE, продуктовые интеграции, экономия токенов и ускорение регресса
требуют отдельного пилота. Исторические архивы и протоколы не являются
подтверждением текущей интеграции. Профильные skills-кандидаты не включаются
по умолчанию в managed-контекст без измеренного основания.

## Лицензия

[MIT](LICENSE). Локальные настройки и рабочие артефакты исключены из публикации.
