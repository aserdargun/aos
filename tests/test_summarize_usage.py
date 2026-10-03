from copy import deepcopy
from decimal import Decimal
import json
import unittest

from scripts.record_usage import FIELDS, REPO_ROOT
from scripts.summarize_usage import counterfactual_cost, render


class UsageSummaryTests(unittest.TestCase):
    def setUp(self):
        self.counts = dict(zip(FIELDS, [1000000, 800000, 0, 100000, 50000, 1100000]))
        self.rates = {'input': '2', 'cached_input': '0.1', 'output': '10'}
        self.prices = {'schema': 'aos.api-price-reference.v1', 'usd_per_million_tokens': {'gpt-6.1-sol': self.rates}}
        self.snapshot = {'schema_version': '1', 'status': 'observed', 'cutoff': '2026-10-03T18:00:00Z',
            'coverage': {'sessions': 1, 'first_usage_event': '2026-10-03T17:00:00Z',
                         'last_usage_event': '2026-10-03T17:59:00Z'},
            'counts': self.counts, 'models': [{'provider': 'openai', 'model': 'gpt-6.1-sol',
                'effort': 'high', 'counts': self.counts}],
            'days': [{'day': '2026-10-03', 'counts': self.counts}]}

    def test_cached_input_and_reasoning_not_double_counted(self):
        self.assertEqual(counterfactual_cost(self.counts, self.rates), Decimal('1.48'))
        self.assertEqual(counterfactual_cost(self.counts | {'reasoning_output_tokens': 0}, self.rates), Decimal('1.48'))
        self.assertIsNone(counterfactual_cost(self.counts, None))
        self.assertIsNone(counterfactual_cost(self.counts | {'cache_write_input_tokens': 1}, self.rates))

    def test_invalid_counts_prices_and_totals_fail_closed(self):
        for counts in (self.counts | {'total_tokens': 0}, self.counts | {'input_tokens': True}):
            with self.assertRaises(ValueError):
                counterfactual_cost(counts, self.rates)
        for rate in ('NaN', 'Infinity', '-1', '100001', 2):
            with self.assertRaises((ValueError, ArithmeticError)):
                counterfactual_cost(self.counts, self.rates | {'input': rate})
        for field in ('models', 'days'):
            wrong = deepcopy(self.snapshot)
            wrong[field] *= 2
            with self.assertRaises(ValueError):
                render(wrong, self.prices)

    def test_unknown_is_unpriced_and_private_metadata_is_not_rendered(self):
        self.snapshot['private_message'] = 'SENSITIVE_SECRET'
        self.snapshot['status'] = 'partial'
        self.snapshot['models'][0]['model'] = 'unknown'
        text = render(self.snapshot, self.prices)
        self.assertIn('Hesaplanamadı', text)
        self.assertIn('fiyatlanamayan token: **1,100,000**', text)
        self.assertNotIn('SENSITIVE_SECRET', text)
        self.assertIn('**Kısmi kayıt:**', text)
        self.snapshot['models'][0]['effort'] = 'SENSITIVE_SECRET'
        with self.assertRaises(ValueError):
            render(self.snapshot, self.prices)

    def test_public_snapshot_matches_readme_block(self):
        snapshot = json.loads((REPO_ROOT / 'docs/usage_latest.json').read_text())
        prices = json.loads((REPO_ROOT / 'docs/usage_prices_20261003.json').read_text())
        readme = (REPO_ROOT / 'README.md').read_text()
        block = readme.split('<!-- aos-usage:start -->\n', 1)[1].split('<!-- aos-usage:end -->', 1)[0]
        self.assertEqual(block, render(snapshot, prices))


if __name__ == '__main__':
    unittest.main()
