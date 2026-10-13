# Фильтр, который возвращает пустую выдачу

Учебное R-1: GET /orders?status=paid возвращает все оплаченные заказы текущего пользователя, сохраняет их ID и исключает остальные. В изолированном наборе ровно ORDER-11 — свой paid, ORDER-12 — свой new, ORDER-13 — чужой paid. Код tests/test_filter.py, ревизия fixture-v1:

```python
assert response.status_code == 200
orders = response.json()['orders']
assert all(row['status'] == 'paid' for row in orders)
assert all(row['owner'] == current_user for row in orders)
```

Запуска нет. Сделай содержательное ревью: приведи неправильный ответ, проходящий проверки, предложи минимальную проверку и источник ожидания. Не заявляй подтверждённый дефект продукта или выполненный эксперимент.
