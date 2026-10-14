# QA-пакет 2.2.0 — подключение проекта через MCP

Stdio MCP расширения теперь предлагает три инструмента. `qa_package_info`
сохраняет прежний контракт; `qa_project_init` и `qa_project_doctor` вызывают
Python-функции напрямую. `/qa:init` и `/qa:doctor` используют MCP в первую очередь,
CLI остаётся fallback при недоступности инструмента.

- Init требует абсолютный `project_root`; `rebind` по умолчанию false.
  Идемпотентная запись ограничена `.gitignore`, `.qa-agent/APP_CONTEXT.md`,
  `.qa-agent/project.local.json`. Существующий контекст сохраняется.
- Перенос требует ответа с прежним/новым путём, показа пользователю и разрешения,
  затем второго вызова с `rebind=true` в той же MCP-сессии. Изменённые пути
  и новая сессия требуют повторного показа. Сервер фиксирует выдачу путей;
  их показ человеку и разрешение переноса обеспечивает агент.
- Ошибки выполнения возвращаются как `isError` с CLI-текстом
  `QA project error: …`, а не теряются в stderr. Ошибки аргументов — JSON-RPC -32602.
- Doctor read-only; `needs_attention` — штатный результат. Единственный
  дочерний процесс — существующий `gigacode --version`.

## Проверки

```bash
python3 -m unittest discover -s tests -p test_extension_project.py -v
python3 -m unittest discover -s tests -v
python3 scripts/package_agent.py --output-dir dist/release-2.2.0 --date 2026-10-14
python3 scripts/package_agent.py --verify dist/release-2.2.0/qa-agent-2.2.0.zip
```

Проверяются stdio-диспетчер, ошибки в MCP-контенте, запрет относительных путей,
идемпотентность/границы записи, двухшаговый перенос и единственный зонд doctor.
Живой GIGACODE CLI в среде разработки отсутствует; тесты не подтверждают его
загрузку новых tools. Пилот — по [EXTENSIONS.md](EXTENSIONS.md),
обновление — [UPGRADE_2.2.0.md](UPGRADE_2.2.0.md).
