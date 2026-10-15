# Передача проверенных данных

Пример создаёт независимое назначение, сохраняет синтетический JSON-набор,
считывает его свойства/связь/доступность и формирует отчёт с доказательствами.
Никакого приложения, API, БД или живой модели нет. Набор остаётся в отдельной
директории до окончания зависимых проверок. Это учебный протокол, не разрешение
создавать подобные сущности в продукте.

Из корня пакета выберите **новую** директорию вне поставки:

```bash
python3 examples/data-readiness/build_demo.py --output-dir /tmp/qa-data-demo
python3 scripts/manage_task.py --state /tmp/qa-data-demo/task.json init --task-id QA-DATA-DEMO --title 'Synthetic data readiness' --criteria /tmp/qa-data-demo/criteria.json
python3 scripts/manage_task.py --state /tmp/qa-data-demo/task.json assign --input /tmp/qa-data-demo/assignment.json --expected-revision 1
python3 scripts/manage_task.py --state /tmp/qa-data-demo/task.json prepare --request-id data-1 --expected-revision 2
python3 scripts/manage_task.py --state /tmp/qa-data-demo/task.json submit --request-id data-1 --expected-revision 3
python3 scripts/manage_task.py --state /tmp/qa-data-demo/task.json review --request-id data-1 --decision 'Synthetic file observations read; no live application checked' --expected-revision 4
```

Результат accepted допускает ready-зависимость для TC-1/TC-2 в течение одного
часа. Координатор проверяет содержание до review; сама команда не заменяет чтение.
Режим `--partial` при создании другой директории меняет владельца неоплаченного
заказа. Наблюдение связи становится failed, готов только TC-1; TC-2 блокируется.
Общий пункт видимости остаётся passed. Генератор не переиспользует существующую
директорию и не продлевает ранее принятые данные.

Доказательства записывают хеш dataset.json и считанные записи. Пакет замораживает
весь набор через inputs: изменение файла после принятия обнаруживается на
следующем переходе. После зависимых проверок удалите только созданную директорию.
Для приложения нужен собственный адаптер проверки доступности, изоляция,
продуктовые источники и безопасная очистка по [DATA_READINESS.md](../../docs/DATA_READINESS.md).
