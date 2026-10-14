# QA Agent — GIGACODE CLI · 2.3.0

Переносимый QA-пакет: координатор, 14 специализированных ролей, 5 skills,
проверяемые контракты результатов и Python-инструменты. Основной профиль —
**GIGACODE CLI от GitVerse**. Общая QA-логика отделена от среды исполнения;
для другого CLI нужны совместимый адаптер и его собственный профиль ролей.

## Быстрый старт

Нужны настроенный GIGACODE CLI, Python 3.10+ и Git. Python-пакеты устанавливать
не нужно: утилиты используют стандартную библиотеку. Поддерживаемая среда
локальных процессов — macOS/Linux, Windows через WSL.

```bash
mkdir -p "$HOME/qa-tools"
git clone https://github.com/dimavrb/QaAgentGigacodeCli.git "$HOME/qa-tools/qa-agent"
python3 --version
gigacode --version
gigacode extensions install "$HOME/qa-tools/qa-agent"
```

Установка принимает локальную папку: Git URL используется только командой
`git clone`. Затем откройте `gigacode` из папки приложения и выполните:

```text
/qa:init
/qa:doctor
/qa Составь тест-план для фильтрации по следующим требованиям: …
```

Для автоматического обновления перед QA-сессией добавьте alias в свой shell:

```bash
alias qa-start='python3 "$HOME/qa-tools/qa-agent/scripts/qa_start.py"'
```

Дальше запускайте `qa-start` из папки приложения. Он проверит локальный clone,
обновит пакет при новой версии, сохранит контекст и откроет GIGACODE.
Подробности и условия переноса обновления — [AUTO_UPDATE.md](docs/AUTO_UPDATE.md).

В новой сессии проверьте `/extensions`, `/memory`, `/agents manage`, `/skills`,
`/help` и `/mcp`. Пилот загрузки целевой сборкой CLI обязателен:
локальные тесты не подтверждают поведение менеджера расширений.
Установка из исходной Git-копии также поддерживается по [INSTALL.md](docs/INSTALL.md).

Начните с задачи по требованиям, без внешних систем. Для реальных проверок
укажите стенд, сборку, данные, разрешённые действия и источники ожиданий.
Модель, авторизация, Jira/sbermock и другие MCP-подключения настраиваются отдельно
в вашей установке; секреты в Git не добавляются. Автоматического опроса Jira нет.

## Как устроен пакет

| Компонент | Назначение |
| --- | --- |
| `docs/COORDINATOR.md` | Общие правила QA-координатора |
| `GIGACODE.md` | Короткая точка входа GIGACODE с импортом координатора |
| `AGENTS.md` | Общие правила работы с репозиторием |
| `.gigacode/agents/` | 14 ролей: анализ, план, импакт-анализ, стенд, данные, заглушки, тестирование, расследование, Jira, автотесты, три направления ревью и отчёт |
| `.gigacode/skills/` | Кейсы, браузер, качество проверок и два профильных кандидата для пилота |
| `.gigacode/commands/qa.md` | Команда `/qa` |
| `.gigacode/settings.json` | Переносимый Playwright MCP-профиль без credentials |
| `qa-runtime.json` | Исполнитель по умолчанию и контракт headless-запуска |
| `gigacode-extension.json`, `GIGACODE_EXTENSION.md` | Манифест и контекст расширения |
| `scripts/qa_project.py`, `scripts/qa_extension_mcp.py` | MCP: метаданные, инициализация проекта и read-only диагностика |
| `scripts/qa_tasks.py`, `docs/TASK_STORAGE.md` | Список/чтение задач, единые пути и восстановление по реестру |
| `scripts/qa_release.py`, `scripts/qa_recover.py`, `docs/UPDATE_SAFETY.md` | Проверка выпуска и независимый rollback вне checkout |
| `scripts/`, `tests/`, `examples/` | Валидаторы, управляемое исполнение, измерения и учебные сценарии |

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
- [Ручные кейсы: JSON → вебхук GitVerse → Jenkins → ТестОпс](examples/testops-jenkins/README.md).
- [Поставка 2.3.0](docs/RELEASE_2.3.0.md), [история](CHANGELOG.md), [план развития](docs/ROADMAP_3.0.md).

## Сохранённые QA-задачи

`/qa:tasks` показывает незавершённые задания проекта, `/qa:resume QA-123`
восстанавливает выбранное по `.qa-agent-state/tasks/*/task.json`. Пустая `/qa`
предлагает выбрать задачу или задать новую цель. Read-only MCP читает состояние;
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
