---
description: Настроить локальный контекст установленного QA-пакета
---

Определи фактический project_root; package_root = <project_root>/.qa-agent/package.
Если install.json/пакета нет, покажи установку из обычного терминала по
PROJECT_INSTALLATION.md. Не запускай глобальные Python-файлы вместо установки.
Вызови `python3 "<package_root>/scripts/qa_project.py" init --project-root "<project_root>"`.
Переносимый install.json уже содержит версию и относительный путь. Init сохраняет
контекст и настройки, добавляет только недостающие локальные файлы/ignore-правила.
Покажи результат и предложи `/qa:doctor`. Не подключай внешние системы автоматически.
