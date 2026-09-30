import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT
from aos.site_page_evidence_cli import main, select_site_page_evidence


PAGE = json.loads((REPO_ROOT / 'examples/site_page_evidence.json').read_text())['page']
COMPARISON = json.loads((REPO_ROOT / 'examples/site_page_comparison.json').read_text())['comparison']
EVIDENCE = {
    'schema_version': '1.0', 'mode': 'read_only_synthetic_page_evidence',
    'run_ref': 'a' * 64, 'snapshot_sha256': 'b' * 64,
    'page_count': 2, 'pages': [PAGE, {**PAGE, 'page_key': 'details',
                                      'source': {**PAGE['source'], 'verification_id': 'other'}}],
    'profile_bound': False, 'execution_authorized': False,
    'collection_authorized': False, 'training_ready': False,
}


class SitePageEvidenceCliTests(unittest.TestCase):
    def test_inspect_selects_one_exact_verification_without_changing_source(self):
        with patch('aos.site_page_evidence_cli.review_site_page_evidence', return_value=EVIDENCE) as review:
            first = select_site_page_evidence(Path('/private/trajectory.sqlite'), 'run-synthetic',
                                              'verification-synthetic')
            second = select_site_page_evidence(Path('/private/trajectory.sqlite'), 'run-synthetic',
                                               'verification-synthetic')
        self.assertEqual(first, second)
        self.assertEqual(first['pages'], [PAGE])
        self.assertEqual(first['page_count'], 1)
        self.assertEqual(EVIDENCE['page_count'], 2)
        self.assertEqual(first['snapshot_sha256'], EVIDENCE['snapshot_sha256'])
        self.assertFalse(first['profile_bound'])
        self.assertEqual(review.call_count, 2)

    def test_inspect_rejects_invalid_missing_and_duplicate_selection(self):
        for verification_id in ('../unsafe', 'missing'):
            with self.subTest(verification_id=verification_id):
                with patch('aos.site_page_evidence_cli.review_site_page_evidence', return_value=EVIDENCE):
                    with self.assertRaises(ValueError):
                        select_site_page_evidence(Path('/private/trajectory.sqlite'), 'run-synthetic',
                                                  verification_id)
        duplicate = {**EVIDENCE, 'pages': [PAGE, PAGE]}
        with patch('aos.site_page_evidence_cli.review_site_page_evidence', return_value=duplicate):
            with self.assertRaises(ValueError):
                select_site_page_evidence(Path('/private/trajectory.sqlite'), 'run-synthetic',
                                          'verification-synthetic')

    def test_inspect_cli_prints_only_selected_metadata(self):
        output = io.StringIO()
        with patch('aos.site_page_evidence_cli.review_site_page_evidence', return_value=EVIDENCE):
            with contextlib.redirect_stdout(output):
                main(['inspect', '--database', '/private/trajectory.sqlite',
                      '--run-id', 'run-synthetic', '--verification-id', 'verification-synthetic'])
        report = json.loads(output.getvalue())
        self.assertEqual(report['page_count'], 1)
        self.assertEqual(report['pages'], [PAGE])
        self.assertNotIn('other', output.getvalue())
        self.assertNotIn('/private/', output.getvalue())

    def test_compare_cli_forwards_exact_selection_without_defaults(self):
        output = io.StringIO()
        with patch('aos.site_page_evidence_cli.compare_site_page_draft',
                   return_value=COMPARISON) as compare:
            with contextlib.redirect_stdout(output):
                main(['compare', '--database', '/private/trajectory.sqlite',
                      '--run-id', 'run-synthetic', '--verification-id', 'verification-synthetic',
                      '--profiles', '/private/profiles', '--store', '/private/pages',
                      '--knowledge-sha256', 'a' * 64, '--selected-profile-sha256', 'b' * 64])
        self.assertEqual(json.loads(output.getvalue()), COMPARISON)
        compare.assert_called_once_with(
            Path('/private/trajectory.sqlite'), 'run-synthetic',
            profiles=Path('/private/profiles'), store=Path('/private/pages'),
            knowledge_sha256='a' * 64, selected_profile_sha256='b' * 64,
            verification_id='verification-synthetic')
        self.assertFalse(COMPARISON['training_ready'])

    def test_private_source_error_and_argument_error_have_no_partial_report(self):
        output = io.StringIO()
        errors = io.StringIO()
        with patch('aos.site_page_evidence_cli.review_site_page_evidence',
                   side_effect=ValueError('secret-private-source')):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                with self.assertRaises(SystemExit) as failure:
                    main(['inspect', '--database', '/private/trajectory.sqlite',
                          '--run-id', 'run-synthetic', '--verification-id', 'verification-synthetic'])
        self.assertEqual(failure.exception.code, 1)
        self.assertEqual(output.getvalue(), '')
        self.assertNotIn('secret-private-source', errors.getvalue())
        with self.assertRaises(SystemExit) as arguments:
            main(['compare', '--database', 'secret-private-source', '--run-id', 'run-synthetic',
                  '--verification-id', 'verification-synthetic'])
        self.assertEqual(str(arguments.exception), 'Site page review unavailable: invalid arguments.')


if __name__ == '__main__':
    unittest.main()
