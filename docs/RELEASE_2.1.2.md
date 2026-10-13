# QA-пакет 2.1.2 — GIGACODE CLI

Дата: 2026-10-13. Состав: 14 ролей, 5 skills и `/qa`. Общие QA-контракты и
Python-утилиты сохранены; профиль GIGACODE вынесен в `.gigacode/`, shared-инструкции
— в `docs/COORDINATOR.md`, контракт запуска — в `qa-runtime.json`.

Требования: Python 3.10+, GIGACODE CLI от GitVerse, POSIX/macOS/Linux или WSL.
Модель, auth и внешние MCP не входят в поставку. Playwright MCP pin 0.0.83 сохранён.
Упаковка включает исходники, инструкции, тесты, примеры и LICENSE; artifacts,
Git/IDE, runtime, caches и локальные overrides исключены.

## Проверки

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s examples/test-data -v
python3 scripts/package_agent.py --output-dir dist --date 2026-10-13
python3 scripts/package_agent.py --verify dist/qa-agent-2.1.2.zip
```

Локальный набор проверяет контракты, фикстуры CLI, процессы, ревью, отмену,
MCP-шлюз, совместимость метрик и упаковку. Конкретный протокол сборки фиксируется
отдельно по фактическому ZIP и результатам команд. При изменении исходников
нужна новая сборка; исторический архив 2.1 сохраняется.

## Пределы

Живой GIGACODE в среде разработки не установлен. Локальная приёмка не подтверждает
семантику флагов установленного CLI, качество модели, реальные интеграции или
ускорение продукта. Preflight останавливает несовместимые headless-запуски;
проверка memory/roles/skills/commands и live-pilot необходимы отдельно.

Переход — [UPGRADE_2.1.2.md](UPGRADE_2.1.2.md), установка — [INSTALL.md](INSTALL.md).
