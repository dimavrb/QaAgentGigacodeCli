---
description: Проверить проектную установку QA Agent без изменения файлов
---

Определи project_root и package_root = <project_root>/.qa-agent/package.
Выполни `python3 "<package_root>/scripts/qa_project.py" doctor --project-root "<project_root>"`.
Код 1 означает needs_attention, код 2 — ошибку. Не исправляй файлы автоматически.
Покажи версию и failed-проверки. Повреждение/несовпадение install.json, pending
установки и смешанные версии требуют рассмотрения по PROJECT_INSTALLATION.md.
Отдельно проверь `/agents manage`, `/skills`, `/help`, `/memory` в новой сессии.
Успех локальных проверок не подтверждает live-загрузку или качество модели;
сохраняй `live_extension_verified=false`. Не запускай продуктовые проверки.
