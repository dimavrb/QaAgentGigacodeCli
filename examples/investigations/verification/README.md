# Пример проверки подозрения

Материалы полностью синтетические. application.py — намеренно ошибочный учебный обработчик; observation.txt и deployment.txt — учебные первичные материалы. Они не описывают приложение пользователя и не подтверждают результат на реальном стенде.

| Вопрос | Что установлено в примере |
| --- | --- |
| Бизнес-действие | Покупатель запрашивает административный список пользователей |
| Правило | R-1 требует HTTP 403 без списка |
| Факт | Учебный GET /admin/users вернул HTTP 200 и пользователя |
| Код | application.py:3 принимает роль, но возвращает список без проверки |
| Применимость | deployment.txt связывает fixture-source-1 с fixture-stage/fixture-1 |
| Выполнение механизма | В учебном trace записана ветка list_users_without_role_check |
| Вердикт | Нарушение и причина рассматриваются отдельно; в этом примере оба подтверждены учебными материалами |
| Следующий шаг | Для этого ограниченного вывода повтор не требуется |

Основной артефакт — verification.json. Его SHA-256 входит в state.json; assessment координатора привязан к состоянию и result.json. Старые примеры работают без нового артефакта.

Из корня QA-пакета:

```bash
python3 scripts/validate_qa_result.py \
  --input examples/investigations/verification/result.json \
  --task-id QA-FIXTURE --request-id QA-FIXTURE-1 \
  --agent qa-defect-investigator \
  --investigation-state examples/investigations/verification/state.json
```

Ожидается код 0: структура и записанная оценка корректны. Это не автоматическая предметная оценка.

Если код недоступен, используй code_path.status=unavailable, null repository/revision, deployment_match=unknown, пустые deployment_evidence_ids/steps и конкретный assessment. Для cause укажи unknown, null scope/statement и пустые evidence_ids; добавь ограничение в артефакт и payload. Достаточное runtime-доказательство сохраняет defect_confirmed. Если исходники другой версии, причина application остаётся suspected/unknown, даже когда нарушение уже подтверждено. После изменения обновляются SHA-256 и содержательная оценка, иначе валидатор отклонит устаревшее принятие.

Не используй verification.json или application.py как runtime-доказательство. Для нового запуска нужен конкретный недостающий факт, условия и критерий остановки; для retest — также repeat_of и причина. Эти ограничения проверяют tests/test_defect_verification.py. Живой запуск GIGACODE и проверка установленного маршрута выполняются отдельно.
