# Проверка силы assert — синтетический пример

Источник учебного ожидания: подготовлен собственный оплаченный заказ 11; выдача должна содержать его и не должна содержать неоплаченные или чужие заказы. Это правило только данного примера. Продукт, API и рабочий CI не запускаются.

Профиль исходного состояния: unittest, текущий пользователь alice, данные создаются заново prepared_response при каждом запуске, внешнего доступа и очистки нет. Базовая выдача корректна; QA_EXAMPLE_FAULT задаёт подмену только на время отдельного процесса. Исходные файлы не меняются.

Из корня QA-пакета:

```bash
python3 -m unittest discover -s examples/autotest-quality -p 'test_*.py' -v
QA_EXAMPLE_FAULT=status python3 -m unittest discover -s examples/autotest-quality -p 'test_*.py' -v
QA_EXAMPLE_FAULT=owner python3 -m unittest discover -s examples/autotest-quality -p 'test_*.py' -v
python3 -m unittest discover -s examples/autotest-quality -p 'test_*.py' -v
```

Первый и последний запуск должны завершиться кодом 0. Подмена status должна дать код 1 и AssertionError с причиной unexpected order status; owner — код 1 и foreign order owner. Падение импорта или ValueError не являются обнаружением этих ошибок. unittest discovery показывает PaidFilterTest.test_paid_rows_belong_to_current_user: это обнаружение учебного теста, не подтверждение его включения в CI приложения.

Для артефакта качества сохрани добавленное покрытие, профиль, обнаруженный test ID, команды, коды и целевые причины падений, подтверждение возврата к корректному ответу и ограничения. Независимое ревью читает реальные результаты запусков. Не копируй учебное ожидаемое поведение или успех в рабочий отчёт.
