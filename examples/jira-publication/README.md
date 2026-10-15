# Синтетическая публикация Jira

Генератор копирует учебное расследование в новую директорию вне поставки,
принимает его через реестр и формирует scope, синтетический навык, поиск,
чтение и настоящие локальные квитанции ActionGateway с подставным клиентом.
Jira, живой навык и модель не запускаются; задача никуда не публикуется.
Изменения оценочных хешей относятся только к новой runtime-копии примера.

```bash
python3 examples/jira-publication/build_demo.py --output-dir /tmp/qa-jira-demo
python3 scripts/manage_task.py --state /tmp/qa-jira-demo/task.json assign --input /tmp/qa-jira-demo/assignment.json --expected-revision 5
python3 scripts/manage_task.py --state /tmp/qa-jira-demo/task.json prepare --request-id jira-1 --expected-revision 6
python3 scripts/manage_task.py --state /tmp/qa-jira-demo/task.json submit --request-id jira-1 --expected-revision 7
python3 scripts/manage_task.py --state /tmp/qa-jira-demo/task.json review --request-id jira-1 --decision 'Synthetic receipts and snapshots read; no Jira action occurred' --expected-revision 8
```

Ожидается accepted на ревизии 9. Для маршрута найденного дубля выберите другую
новую директорию и добавьте генератору --duplicate; gateway останется без write.
Обычный маршрут использует один вызов подставного create и чтение полей.
Подготовка отчёта здесь предшествует prepare, как у остальных готовых фикстур;
в реальном запуске внешние действия выполняет назначенный исполнитель после
принятия зависимостей, через согласованный шлюз и установленный навык.

Удаление чтения, изменение summary, неполный поиск, неверный ключ/проект,
несовпадение версии расследования или квитанции препятствуют submit.
Контракт и границы проверки — [JIRA_PUBLICATION.md](../../docs/JIRA_PUBLICATION.md).
