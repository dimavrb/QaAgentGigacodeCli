# QA-пакет 2.1.4 — GIGACODE extension

Дата: 2026-10-14. Поставка: 14 ролей, 5 skills, `/qa`, `/qa:init`, `/qa:doctor`,
Python-утилиты и read-only stdio MCP метаданных. Версия extension и VERSION
проверяются при сборке и проверке ZIP. Требуется Python 3.10+; зависимости
Python не добавлены. Playwright остаётся отдельным шаблоном настройки.

`qa_project.py init` связывает проект с установленным пакетом и сохраняет
существующий контекст. `doctor` проверяет структуру, привязку версии и доступность
CLI без изменения проекта. Перенос на другой путь требует явного rebind.
Роли, skills и команды различают package_root/project_root; проект не получает
копии исходников extension при инициализации.

## Проверки

```bash
python3 -m unittest discover -s tests -p 'test_extension_project.py' -v
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s examples/test-data -v
python3 scripts/package_agent.py --output-dir dist/release-2.1.4 --date 2026-10-14
python3 scripts/package_agent.py --verify dist/release-2.1.4/qa-agent-2.1.4.zip
```

Локальные проверки покрывают сохранность проекта при повторной инициализации,
обновлении и переносе, изоляцию двух проектов, работу из чужого cwd с Unicode/
пробелами, отказ при некорректных привязках/ссылках и stdio MCP без записи файлов.

## Пределы

Живой GIGACODE не установлен в среде разработки. Обнаружение всех компонентов,
custom paths, подстановка `${extensionPath}`, менеджер установки/обновления и
качество QA-задачи требуют пилота на целевой сборке CLI по
[EXTENSIONS.md](EXTENSIONS.md). `configured` и `ready_for_cli_check` подтверждают
локальные условия; серверная интеграция и `live_extension_verified` ими не подтверждены.

Переход с 2.1.3 — [UPGRADE_2.1.4.md](UPGRADE_2.1.4.md).
