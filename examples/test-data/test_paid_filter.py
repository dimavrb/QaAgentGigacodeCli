"""Run with: python3 -m unittest discover -s examples/test-data -v."""
import unittest

from data_recipe import Store, assert_filter_result, prepared_filter_data, select_paid_for_owner


class PaidFilterExample(unittest.TestCase):
    def test_only_prepared_own_paid_order_is_returned(self):
        store = Store()
        with prepared_filter_data(store, 'demo-worker-1-filter-attempt-1') as data:
            result = select_paid_for_owner(store.orders.values(), data.own_paid.owner)
            assert_filter_result(result, data)
        self.assertEqual({}, store.orders)


if __name__ == '__main__':
    unittest.main()
