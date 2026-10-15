# Учебная готовность по всем пунктам админгайда

Пример создаёт новый снимок и синтетические наблюдения двух пунктов гайда.
Стенд, модель и внешние инструменты не запускаются. Для реальной готовности
нужны реальный полный гайд, фактическое выполнение и содержательная приёмка.

```bash
python3 examples/environment-readiness/build_demo.py --output-dir /tmp/qa-env-demo
python3 scripts/manage_task.py --state /tmp/qa-env-demo/task.json init --task-id QA-ENV-DEMO --title "Учебный админгайд" --criteria /tmp/qa-env-demo/criteria.json
python3 scripts/manage_task.py --state /tmp/qa-env-demo/task.json assign --input /tmp/qa-env-demo/assignment.json --expected-revision 1
python3 scripts/manage_task.py --state /tmp/qa-env-demo/task.json prepare --request-id env-1 --expected-revision 2
python3 scripts/manage_task.py --state /tmp/qa-env-demo/task.json submit --request-id env-1 --expected-revision 3
python3 scripts/manage_task.py --state /tmp/qa-env-demo/task.json review --request-id env-1 --decision "Проверены учебный гайд и оба синтетических наблюдения" --expected-revision 4
```

Ожидается accepted на ревизии 6 и presentation.status=ready: после принятия
отдельно сохранена ссылка на result.environment.html. Откройте этот HTML —
в нём видны результаты каждого пункта и отсутствие открытых вопросов.
Это принятие учебного артефакта, не готовность
реального стенда и не finish всей задачи. Отчёт подготовлен builder заранее;
prepare сам не выполняет действия и не запускает агента.

Удаление guide_checks, любого пункта, доказательства или замена product_version
препятствует submit. Для экспериментов создавайте новую временную директорию;
после submit исходный результат не редактируйте. Контракт —
[ENVIRONMENT_READINESS.md](../../docs/ENVIRONMENT_READINESS.md).

Для примера с оставшимся вопросом создайте другую директорию:
`python3 examples/environment-readiness/build_demo.py --output-dir /tmp/qa-env-questions --with-questions`.
Пройдите те же команды реестра с новым путём. После принятия правдивого blocked
страница отобразит невыполненный ADM-2 и вопрос об адресе API; готовность стенда
и finish задачи по-прежнему не подтверждены.
