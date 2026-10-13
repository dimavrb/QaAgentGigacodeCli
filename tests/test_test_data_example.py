import importlib.util
import sys
import unittest
from pathlib import Path


EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/test-data/data_recipe.py'
spec = importlib.util.spec_from_file_location('qa_synthetic_data_recipe', EXAMPLE)
recipe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = recipe
spec.loader.exec_module(recipe)


class TestDataRecipeTests(unittest.TestCase):
    def test_overlapping_attempts_have_independent_owners_and_cleanup(self):
        store = recipe.Store()
        with recipe.prepared_filter_data(store, 'run-1-worker-1-test-1-attempt-1') as first:
            with recipe.prepared_filter_data(store, 'run-2-worker-1-test-1-attempt-1') as second:
                self.assertNotEqual(first.own_paid.owner, second.own_paid.owner)
                self.assertEqual(6, len(store.orders))
                recipe.assert_filter_result(recipe.select_paid_for_owner(store.orders.values(), first.own_paid.owner), first)
                recipe.assert_filter_result(recipe.select_paid_for_owner(store.orders.values(), second.own_paid.owner), second)
            self.assertEqual(3, len(store.orders))
            self.assertIn(first.own_paid.id, store.orders)
        self.assertEqual({}, store.orders)

    def test_partial_setup_failure_cleans_created_records_and_preserves_unrelated(self):
        class FailingStore(recipe.Store):
            def create(self, namespace, key, owner, status):
                if key == 'foreign-paid':
                    raise RuntimeError('Simulated failure of third creation')
                return super().create(namespace, key, owner, status)

        store = FailingStore()
        unrelated = store.create('unrelated', 'order', 'other', 'paid')
        with self.assertRaisesRegex(RuntimeError, 'third creation'):
            with recipe.prepared_filter_data(store, 'attempt'):
                self.fail('Incomplete setup must not reach the test')
        self.assertEqual({unrelated.id: unrelated}, store.orders)

    def test_test_failure_cleans_data_and_recipe_can_be_repeated(self):
        store = recipe.Store()
        with self.assertRaisesRegex(AssertionError, 'test failed'):
            with recipe.prepared_filter_data(store, 'attempt'):
                raise AssertionError('test failed')
        self.assertEqual({}, store.orders)
        with recipe.prepared_filter_data(store, 'attempt') as data:
            recipe.assert_filter_result(recipe.select_paid_for_owner(store.orders.values(), data.own_paid.owner), data)
        self.assertEqual({}, store.orders)

    def test_collision_does_not_delete_existing_attempt(self):
        store = recipe.Store()
        with recipe.prepared_filter_data(store, 'attempt') as data:
            original = dict(store.orders)
            with self.assertRaisesRegex(ValueError, 'Namespace collision'):
                with recipe.prepared_filter_data(store, 'attempt'):
                    self.fail('Colliding setup must not reach the test')
            self.assertEqual(original, store.orders)
            self.assertIn(data.own_paid.id, store.orders)
        self.assertEqual({}, store.orders)

    def test_counterexamples_detect_empty_missing_status_and_missing_owner_filters(self):
        store = recipe.Store()
        with recipe.prepared_filter_data(store, 'attempt') as data:
            incorrect = [[], [data.own_paid, data.own_unpaid], [data.own_paid, data.foreign_paid]]
            for result in incorrect:
                with self.subTest(ids=[order.id for order in result]), self.assertRaises(AssertionError):
                    recipe.assert_filter_result(result, data)
            recipe.assert_filter_result([data.own_paid], data)

    def test_invalid_created_precondition_never_reaches_test_and_is_cleaned(self):
        class IncorrectStore(recipe.Store):
            def create(self, namespace, key, owner, status):
                return super().create(namespace, key, owner, 'unpaid')

        store = IncorrectStore()
        with self.assertRaisesRegex(ValueError, 'precondition'):
            with recipe.prepared_filter_data(store, 'attempt'):
                self.fail('Unverified data must not reach the test')
        self.assertEqual({}, store.orders)


if __name__ == '__main__':
    unittest.main()
