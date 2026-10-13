# Установка QA-агента 2.1.1 в GIGACODE CLI

Требуются установленный и авторизованный **GIGACODE CLI от GitVerse**, Python
3.10+ и Git. Установка самого CLI и доступ к модели выполняются по правилам вашей
организации. Не используйте одноимённые CLI других разработчиков.

## Подготовка

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
`/agents manage` (14 ролей `qa-*`), `/skills` (5 навыков) и `/help` (команда `/qa`).
Именованные субагенты должны возвращать результат координатору; fork-режим сам
по себе не является независимым ревью и не обеспечивает приёмку в реестре.

Первое задание: `/qa Составь тест-план по следующим требованиям: …`.
Если требования неполны, ожидается конкретный список вопросов. Реальное
исполнение продукта требует стенда, версии, данных и разрешённых инструментов.

## Настройка среды

Модель, auth и личные MCP настраивайте в своей установке GIGACODE. Поставка
содержит только Playwright MCP-шаблон с pin 0.0.83. Перед UI-работой проверьте
доступные tools, общий browser_pool_path и cleanup по [BROWSER_PROFILES.md](BROWSER_PROFILES.md).
Jira требует отдельного навыка `jira-defect-creator` и доступа. Для sbermock
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
