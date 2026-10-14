# QA-пакет 2.3.0 — защита обновления и проектных операций

Выпуск сохраняет поиск/продолжение задач из 2.2.1 и исправляет пять сценариев
промышленного ревью 2.2.0:

- Кандидат проверяется до fast-forward: ресурсы, синтаксис, зависимости,
  полный manifest, fresh imports, launcher help и MCP initialize. Для main
  разрешённого GitHub-источника требуется успешный CI точного SHA.
- Внешний recovery zipapp и проверяемый snapshot установки дают независимый
  rollback при поломке launcher/частичной активации и продолжение прерванного отката.
- Контекст создаётся без перезаписи; init/configure сериализуются общей блокировкой.
- Rebind сравнивает полный показанный binding и пишет новый под одной блокировкой.
  CLI требует `--expected-binding-sha256`; библиотека — snapshot/digest.
- JSON-привязка проверяется до чтения; FIFO/симлинки отвергаются. MCP сохраняет
  защиту от глубокого/длинного JSON и некорректного Unicode из 2.2.1.

Версия minor отражает новые обязательные условия CI/rollback и более строгий
CLI rebind. Старые pending без backup/helper требуют отдельного согласованного
завершения; слепая миграция состояния запрещена.

## Проверки

```bash
python3 -m unittest discover -s tests -p test_project_hardening.py -v
python3 -m unittest discover -s tests -p test_update_hardening.py -v
python3 -m unittest discover -s tests -v
python3 scripts/package_agent.py --output-dir dist/release-2.3.0 --date 2026-10-14
python3 scripts/package_agent.py --verify dist/release-2.3.0/qa-agent-2.3.0.zip
```

Регрессия включает отсутствующую зависимость, invalid syntax/import, отказ CI,
частичную установку, поломанный launcher, повреждённый backup, dirty source,
посторонние правки, активные сессии/managed work, прерывания markers/renames,
сохранность контекста/прав и конкурентную перепривязку.

Нативный менеджер и GitHub CI в интеграционных тестах — явно отмеченные фикстуры.
GIGACODE отсутствует в среде разработки: промышленная приёмка требует живого
install/update/rollback с registry readback и проверки файловой системы.
fsync/fault injections не подменяют физический power-loss тест.
Порядок/пределы — [UPDATE_SAFETY.md](UPDATE_SAFETY.md), переход — [UPGRADE_2.3.0.md](UPGRADE_2.3.0.md).
