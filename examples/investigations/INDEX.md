# Контрольные расследования

Все материалы синтетические. Читай только нужную сцену; состояние и отчёт лежат в соответствующей папке. Учебный источник — `requirements.md`.

| Сцена | Ожидаемая проверка |
| --- | --- |
| `confirmed/` | Запись ревью проходит; HTTP 200 нарушает учебное требование HTTP 403 |
| `not-reproduced/` | Ревью проходит для ограниченного вывода; одна попытка не исключает дефект |
| `expected-behavior/` | Ревью проходит; HTTP 403 соответствует требованию |
| `blocked/` | Форма blocked и состояние корректны; обязательная проверка остаётся заблокированной |
| `unsupported/` | Принятие отклоняется: отчёт заявляет HTTP 200, доказательство содержит HTTP 403 |
| `verification/` | Новая проверка подозрения: правило, первичный ответ/trace, учебный handler, связь ревизии со сборкой и отдельный статус причины |

Для продолжения сцены `not-reproduced` из корня проекта:

```bash
python3 scripts/validate_investigation_state.py --input examples/investigations/not-reproduced/state.json --task-id QA-FIXTURE --current-context examples/investigations/not-reproduced/current-context.json --request-id QA-FIXTURE-2
```

Ожидается `resume_ready`, открытая H1 и запрет повтора A1 без причины. Изменение product_version или data_revision в копии current-context даёт `revalidate_conditions` и код 3. Это чтение учебных записей, без браузера и внешнего стенда. Полный порядок — в `docs/INVESTIGATION.md`.
