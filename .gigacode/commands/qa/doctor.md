---
description: Проверить подключение QA Agent extension без изменения проекта
---

Получи метаданные через MCP `qa_package_info` и фактический абсолютный
`project_root` из текущего рабочего проекта. Вызови read-only MCP
`qa_project_doctor` с `{"project_root":"<project_root>"}`. Относительный путь
недопустим. Инструмент вызывает `doctor()` напрямую; единственный дочерний
процесс — существующий зонд `gigacode --version`.
`needs_attention` — штатный результат с failed-проверками, а не ошибка MCP.
Ошибки чтения/структуры приходят как `isError` с `QA project error: …`.
При недоступности инструмента используй CLI fallback:
`python3 "<project_tool>" doctor --project-root "<project_root>"`;
код 1 означает `needs_attention`, код 2 — ошибку. Не обходи ошибку MCP через CLI.
Если недоступен и `qa_package_info`, возьми проверенный installed path из
менеджера расширений и вызови `python3 "<package_root>/scripts/qa_project.py" info`.
Не угадывай путь и не запускай одноимённый скрипт приложения.

Не изменяй файлы и не исправляй конфигурацию автоматически. Сообщи результаты
проверок, версию пакета и следующие действия для failed. Отдельно в текущей
сессии проверь `/extensions`, `/agents manage`, `/skills`, `/help` и `/mcp`:
MCP/CLI-диагностика не подтверждает, что CLI действительно загрузил все компоненты.
Не запускай продуктовые проверки и не обращайся к Jenkins/ТестОпс ради диагностики
установки. Укажи `live_extension_verified=false`, пока нет фактического пилота.
