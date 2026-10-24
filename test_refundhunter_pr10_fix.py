"""Offline regression tests; run in the RefundHunter repo after applying the patch."""
import ast
import datetime
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


def load_function(file, function, ns):
    tree = ast.parse(Path(file).read_text())
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function)
    exec(compile(ast.Module(body=[node], type_ignores=[]), file, 'exec'), ns)
    return ns[function]


class Model:
    def __init__(self, **kw): self.__dict__.update(kw)


class FixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location('rh_store_test', 'store.py')
        cls.store = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.store)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store.DB_PATH = str(Path(cls.tmp.name) / 'test.db')
        ns = {'date': datetime.date}
        cls.valid_date = staticmethod(load_function('agent.py', 'valid_purchase_date', ns))
        policy_ns = {'date': datetime.date, 'datetime': datetime.datetime, 'timezone': datetime.timezone,
                     'timedelta': datetime.timedelta, 'POLICIES': {}, 'PolicyRequest': Model, 'PolicyResult': Model}
        policy_text = Path('policy_agent.py').read_text()
        policy_ns['POLICIES'] = ast.literal_eval(next(n.value for n in ast.parse(policy_text).body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'POLICIES' for t in n.targets)))
        load_function('policy_agent.py', 'parse_purchase_date', policy_ns)
        cls.evaluate = staticmethod(load_function('policy_agent.py', 'evaluate_policy', policy_ns))

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def test_input_date(self):
        self.assertTrue(self.valid_date('2026-10-10'))
        self.assertTrue(self.valid_date(''))
        self.assertFalse(self.valid_date('2026-02-30'))
        self.assertFalse(self.valid_date('3000-01-01'))
        self.assertFalse(self.valid_date('2026/10/10'))

    def test_no_false_merchant_eligibility(self):
        for purchase in ['2026-10-10', '2026-08-01']:
            req = Model(item_id='it_abcdef12', store='amazon', purchase_date=purchase, price_paid=100, current_price=80)
            r = self.evaluate(req, datetime.date(2026, 10, 24))
            self.assertEqual(r.status, 'unknown')
            self.assertIsNone(r.eligible)

    def test_unsupported(self):
        r = self.evaluate(Model(item_id='x', store='other', price_paid=100, current_price=50, purchase_date='2026-10-10'), datetime.date(2026,10,24))
        self.assertEqual(r.status, 'unsupported')

    def test_migration_and_request_correlation(self):
        old = sqlite3.connect(self.store.DB_PATH)
        old.execute('CREATE TABLE items (item_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, product_url TEXT NOT NULL, store TEXT NOT NULL, name TEXT NOT NULL DEFAULT "", price_paid REAL NOT NULL, current_price REAL, checked_at TEXT NOT NULL DEFAULT "", added_at TEXT NOT NULL)')
        old.commit(); old.close()
        self.store.add_item('it_12345678', 'user', 'https://example.com', 'amazon', 100, purchase_date='2026-10-10')
        item = self.store.get_item('it_12345678')
        self.assertEqual(item['purchase_date'], '2026-10-10')
        self.store.set_policy_request('it_12345678', 'new')
        self.assertTrue(self.store.is_current_policy_request('it_12345678', 'new'))
        self.assertFalse(self.store.is_current_policy_request('it_12345678', 'old'))

    def test_model_and_coordinator_wiring(self):
        model = Path('models.py').read_text()
        agent = Path('agent.py').read_text()
        self.assertIn('request_id: str', model)
        self.assertIn('store.set_policy_request(item["item_id"], req.request_id)', agent)
        self.assertIn('store.is_current_policy_request(result.item_id, result.request_id)', agent)
        self.assertIn('purchase_date=rec.get("purchase_date") or ""', agent)

if __name__ == '__main__': unittest.main(verbosity=2)
