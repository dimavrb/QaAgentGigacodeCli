"""Synthetic response fixture: no application, service or CI is exercised."""
import os
import unittest


def prepared_response(fault=None):
    """Rebuild independent data on every call; fault exists only in this fixture."""
    rows = [{'id': 11, 'status': 'paid', 'owner': 'alice'}]
    if fault == 'status':
        rows.append({'id': 12, 'status': 'new', 'owner': 'alice'})
    elif fault == 'owner':
        rows.append({'id': 13, 'status': 'paid', 'owner': 'bob'})
    elif fault is not None:
        raise ValueError('Unknown synthetic fault')
    return rows


class PaidFilterTest(unittest.TestCase):
    def test_paid_rows_belong_to_current_user(self):
        rows = prepared_response(os.environ.get('QA_EXAMPLE_FAULT'))
        self.assertIn(11, [row['id'] for row in rows], 'missing own paid order')
        self.assertTrue(all(row['status'] == 'paid' for row in rows), 'unexpected order status')
        self.assertTrue(all(row['owner'] == 'alice' for row in rows), 'foreign order owner')


if __name__ == '__main__':
    unittest.main()
