# QA-пакет 2.1.5 — локальная установка и автообновление

Дата: 2026-10-14. Профиль: 14 ролей, 5 skills, `/qa`, `/qa:init`, `/qa:doctor`.
Python 3.10+, стандартная библиотека, Git, macOS/Linux или WSL.

Установка теперь требует локального clone и передачи его пути менеджеру.
`qa-start` проверяет новую версию перед сессией, применяет только fast-forward,
вызывает штатный update из этого источника и освежает локальную привязку.
Контекст и доказательства не заменяются. Shared/exclusive lock защищает связанные
сессии; durable intent и readback позволяют восстановиться после сбоя применения.

## Проверки

```bash
python3 -m unittest discover -s tests -p 'test_auto_update.py' -v
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s examples/test-data -v
python3 scripts/package_agent.py --output-dir dist/release-2.1.5 --date 2026-10-14
python3 scripts/package_agent.py --verify dist/release-2.1.5/qa-agent-2.1.5.zip
```

Фикстуры проверяют настоящий локальный Git и интерфейс native update: успешную
смену версии, офлайн, dirty/diverged source, локальные адаптации, активные задачи,
частичное применение, восстановление, readback без повтора и reexec нового кода.
Менеджер GIGACODE в этих тестах является фикстурой; живой CLI здесь отсутствует.
Поддержку локального install/update и точный формат менеджера подтвердите в пилоте.

Переход — [UPGRADE_2.1.5.md](UPGRADE_2.1.5.md), запуск —
[AUTO_UPDATE.md](AUTO_UPDATE.md), установка — [EXTENSIONS.md](EXTENSIONS.md).
