"""Synthetic in-memory data recipe; no application, API or database access."""
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class Order:
    id: str
    owner: str
    status: str


class Store:
    def __init__(self):
        self.orders = {}

    def create(self, namespace, key, owner, status):
        if not namespace or not owner or status not in {'paid', 'unpaid'}:
            raise ValueError('Invalid synthetic precondition')
        identifier = f'{namespace}:{key}'
        if identifier in self.orders:
            raise ValueError('Namespace collision; existing data must not be reused blindly')
        order = Order(identifier, owner, status)
        self.orders[identifier] = order
        return order

    def delete(self, identifier):
        self.orders.pop(identifier, None)


@dataclass(frozen=True)
class FilterData:
    own_paid: Order
    own_unpaid: Order
    foreign_paid: Order


@contextmanager
def prepared_filter_data(store, namespace):
    """Register each created ID immediately; clean partial setup and failed tests."""
    created = []

    def add(key, owner, status):
        order = store.create(namespace, key, owner, status)
        created.append(order.id)
        if store.orders.get(order.id) != Order(f'{namespace}:{key}', owner, status):
            raise ValueError('Created data does not satisfy the precondition')
        return order

    try:
        # Stable business values; namespace belongs to one run/worker/test/attempt.
        own_paid = add('own-paid', f'{namespace}:owner', 'paid')
        own_unpaid = add('own-unpaid', own_paid.owner, 'unpaid')
        foreign_paid = add('foreign-paid', f'{namespace}:other-owner', 'paid')
        yield FilterData(own_paid, own_unpaid, foreign_paid)
    finally:
        # This in-memory backend cannot fail deletion; real cleanup must record errors.
        for identifier in reversed(created):
            store.delete(identifier)


def select_paid_for_owner(orders, owner):
    """Reference behavior of this example only, not an application contract."""
    return [order for order in orders if order.owner == owner and order.status == 'paid']


def assert_filter_result(orders, data):
    observed = list(orders)
    assert len(observed) == 1, 'Expected exactly one prepared own paid order'
    assert observed[0].id == data.own_paid.id, 'Expected the prepared order'
    assert observed[0].owner == data.own_paid.owner, 'Unexpected owner'
    assert observed[0].status == 'paid', 'Unexpected status'
