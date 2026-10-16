# Профиль GIGACODE и проектная установка

С 2.3.13 основной способ подключения — [PROJECT_INSTALLATION.md](PROJECT_INSTALLATION.md).
Пакет содержит 14 ролей, 6 skills и пять команд: /qa, /qa:init, /qa:doctor,
/qa:tasks, /qa:resume. Собственный QA MCP-сервер удалён; операции выполняются
локальными скриптами внутри проекта.

## Native discovery

Для совместимости native discovery сохранены корневые agents/, skills/, commands/
и gigacode-extension.json без mcpServers. Канонические исходники — .gigacode/agents,
.gigacode/skills и .gigacode/commands. Корневые копии должны полностью совпадать:

```bash
python3 scripts/extension_resources.py --apply
python3 scripts/extension_resources.py
```

Сборка, package_info и preflight проверяют совпадение, включая вложенные скрипты
skills. Native install не заменяет проектную установку: глобальный Python-путь
может быть недоступен агенту. Проектный профиль и пакет устанавливаются вместе
через qa_install.py, без обращения к менеджеру расширений.

## Если агенты extension отсутствуют в agents manage

Вместо отдельного копирования только ролей установите полный проектный комплект
по PROJECT_INSTALLATION.md. Прежний qa_project_agents.py сохранён для старых
установок; он не меняет профиль, управляемый новым install.json.
Проверьте /agents manage, /skills, /help и /memory после перезапуска GIGACODE.

## Ручная переустановка если update требует uninstall

Для перехода на 2.3.13 native update/uninstall/install больше не является основным
путём. Установите проектную копию и отключите старый глобальный профиль по
UPGRADE_2.3.13.md. Штатное отключение/удаление выполняется по --help вашей сборки;
не редактируйте внутренний registry. Установщик проекта его не изменяет.

## Проектная область как это устроено в Qwen Code

Для QA-пакета область определяется фактическим размещением проектных файлов.
Флаги другого клиента не доказывают поддержку вашей сборкой GIGACODE. Манифест
сам по себе не задаёт проектную область; проверяйте реальные роли/skills и memory.
Официальные пути GIGACODE: [агенты](https://gitverse.ru/docs/ai/ai-assistant-gigacode/gigacode-cli/subagents),
[skills](https://gitverse.ru/docs/ai/ai-assistant-gigacode/gigacode-cli/skills).
