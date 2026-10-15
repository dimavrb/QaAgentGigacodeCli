# Установка QA-агента 2.3.12 в GIGACODE CLI

Требуются установленный и авторизованный **GIGACODE CLI от GitVerse**, Python
3.10+ и Git 2.36+ для защищённого автообновления/отката. Установка самого CLI и доступ к модели выполняются по правилам вашей
организации. Не используйте одноимённые CLI других разработчиков.

## Подготовка

Если пакет нужен только текущему проекту, используйте
[проектное подключение](IMPORT_INTO_APP.md#подключение-только-в-одном-проекте).
Запуск native install из папки проекта не подтверждает такую область установки.

Основной способ распространения — native extension из локальной Git-копии.
Сначала сохраните пакет отдельно от продукта:

```bash
mkdir -p "$HOME/qa-tools"
git clone https://github.com/dimavrb/QaAgentGigacodeCli.git "$HOME/qa-tools/qa-agent"
gigacode extensions install "$HOME/qa-tools/qa-agent"
```

Эта команда предназначена для первой установки. Если расширение уже установлено
и `extensions update qa-agent` требует uninstall, примените
[порядок переустановки](EXTENSIONS.md#ручная-переустановка-если-update-требует-uninstall)
с резервной копией и остановкой активной работы.

Git URL не передавайте в install. Если используете менеджер внутри GIGACODE,
выберите установку и укажите полный локальный путь к этой папке. Например,
при поддержке такой командной формы:

```text
/extensions install /absolute/path/to/qa-agent
```

Откройте новую сессию из папки приложения:

```text
/qa:init
/qa:doctor
```

CLI подключает контекст и stdio MCP по `gigacode-extension.json`, а роли, skills
и команды обнаруживает в корневых `agents/`, `skills/`, `commands/`. Поля
`agents` / `skills` / `commands` в манифесте не заменяют эти каталоги.
В `/mcp` должны быть пять инструментов: `qa_package_info`, `qa_project_init`,
`qa_project_doctor`, `qa_tasks_list`, `qa_task_get`. `/qa:init` и `/qa:doctor` прежде всего вызывают MCP;
CLI fallback используется только при недоступности соответствующего инструмента.
Передавайте абсолютный `project_root`. Init идемпотентен и пишет только
`.gitignore`, `.qa-agent/APP_CONTEXT.md`, `.qa-agent/project.local.json`, сохраняя
существующий контекст. Перенос пакета: сначала вызов без `rebind`, показ старого/нового
пути и разрешение переноса, затем `rebind=true` в той же MCP-сессии.
Ошибки приходят как `isError` с `QA project error: …`. Doctor read-only:
`needs_attention` — штатный результат, единственный дочерний вызов —
существующий зонд `gigacode --version`. Личные настройки не копируются из пакета.
Сверьте версию в менеджере с `2.3.12`. Откройте новую сессию и проверьте
`/extensions`, `/agents manage`, `/skills`, `/help`, `/mcp` и `/qa`.
Подробности, установка локальной копии и пилот — [EXTENSIONS.md](EXTENSIONS.md).
Если QA-роли не появились, сначала проверьте полный пакет 2.3.12 и его корневые
ресурсы по [EXTENSIONS.md](EXTENSIONS.md#раскладка-ресурсов-расширения).
При сохранении проблемы в `/agents manage` активного extension примените
[проектное подключение ролей](EXTENSIONS.md#если-агенты-extension-отсутствуют-в-agents-manage)
через отдельный `qa_project_agents.py`. Наличие файлов в пакете не равно загрузке CLI.
Первый `--apply` сохраняет историю исходников; дальше `qa-start` автоматически
обновляет неизменённые проектные роли до новой сессии. Локальные правки
сохраняются, конфликты рассматриваются отдельно. При прямом запуске GIGACODE
нужен явный preview/`--apply` по [PROJECT_AGENT_UPDATES.md](PROJECT_AGENT_UPDATES.md).
Для автообновления настройте `qa-start` по [AUTO_UPDATE.md](AUTO_UPDATE.md).
CI gate, backup, внешнее восстановление и новый CLI rebind digest описаны в
[UPDATE_SAFETY.md](UPDATE_SAFETY.md). При переносе CLI требует
`--rebind --expected-binding-sha256 <SHA-256 из первой ошибки>`.
Для сохранённых заданий используйте `/qa:tasks` и `/qa:resume <ID>`;
пустая `/qa` предлагает выбор задачи или новой цели. Пути материалов и порядок
восстановления — [TASK_STORAGE.md](TASK_STORAGE.md). Локальные реестры не
синхронизируются через Git; `/qa:init` не переносит старые задачи автоматически.

### Исходная Git-копия

Клонируйте весь репозиторий, включая `.gigacode/`, либо распакуйте проверенный
ZIP в новый каталог. `docs/`, `scripts/`, `tests/` и `examples/` являются частями
пакета; копирования только файлов ролей недостаточно.

```bash
git clone https://github.com/dimavrb/QaAgentGigacodeCli.git
cd QaAgentGigacodeCli
python3 --version
gigacode --version
python3 -m unittest discover -s tests -v
gigacode
```

В интерактивной сессии проверьте `/memory` (импорт `docs/COORDINATOR.md`),
`/agents manage` (14 ролей `qa-*`), `/skills` (6 навыков) и `/help` (команда `/qa`).
Именованные субагенты должны возвращать результат координатору; fork-режим сам
по себе не является независимым ревью и не обеспечивает приёмку в реестре.

Первое задание: `/qa Составь тест-план по следующим требованиям: …`.
Если требования неполны, ожидается конкретный список вопросов. Реальное
исполнение продукта требует стенда, версии, данных и разрешённых инструментов.

## Настройка среды

Модель, auth и личные MCP настраивайте в своей установке GIGACODE. Поставка
содержит MCP метаданных, инициализации и диагностики extension и отдельный Playwright MCP-шаблон
с pin 0.0.83; Playwright не подключается автоматически манифестом. Перед UI-работой проверьте
доступные tools, общий browser_pool_path и cleanup по [BROWSER_PROFILES.md](BROWSER_PROFILES.md).
Jira-навык [jira-defect-creator](../.gigacode/skills/jira-defect-creator/SKILL.md)
включён в поставку. Для работы настройте подключение и отдельный измеренный
профиль целевого проекта; credentials и профиль храните вне поставки. Для sbermock
нужно существующее фактическое MCP-соединение по [STUBS.md](STUBS.md).

Персональные настройки храните в пользовательском профиле CLI либо игнорируемых
локальных файлах. Пакет не читает `.gigacode/settings.local.json` самостоятельно:
проверьте поддержку локальных overrides вашей установкой. Рабочее состояние,
credentials, browser storage и доказательства не коммитьте.

## Неинтерактивное исполнение

Настройки runner находятся в `qa-runtime.json`. Локальный override можно сохранить
в `qa-runtime.local.json` и передать через `--runtime-config`.

```bash
python3 scripts/agent_runtime.py --mode automation
python3 scripts/agent_runtime.py --mode isolated
python3 scripts/agent_runtime.py --mode gateway
```

Эта проверка не запускает модель. Несовместимые флаги дают ошибку; они не удаляются
для обхода ограничений. Семантика bare/MCP/tool restrictions и JSON-ответа требует
контрольного прогона на конкретной сборке GIGACODE. Подробности — [RUNTIMES.md](RUNTIMES.md).

Для автотестов в другом репозитории подготовьте отдельную рабочую копию и задание
по [образцу](../examples/task-automation.md). Задание и состояние храните вне
репозитория автотестов; результат `ready_for_review` требует независимой приёмки.

```bash
python3 scripts/run_test_automation.py \
  --repo /absolute/path/to/autotests \
  --task-file /absolute/path/to/task.md \
  --output /absolute/path/to/result.json
```

Для подключения к приложению — [IMPORT_INTO_APP.md](IMPORT_INTO_APP.md).

Основание форматов: [память](https://gitverse.ru/docs/ai/ai-assistant-gigacode/gigacode-cli/memory),
[субагенты](https://gitverse.ru/docs/ai/ai-assistant-gigacode/gigacode-cli/subagents),
[skills](https://gitverse.ru/docs/ai/ai-assistant-gigacode/gigacode-cli/skills),
[команды](https://gitverse.ru/docs/ai/ai-assistant-gigacode/gigacode-cli/commands),
[MCP](https://gitverse.ru/docs/ai/ai-assistant-gigacode/gigacode-cli/mcp).
