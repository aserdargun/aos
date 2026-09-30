from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, digest
from aos.dataset import validator
from aos.remote_form_json_submission import (RemoteFormJSONSubmissionPlan,
                                             RemoteFormJSONSubmissionReport,
                                             main,
                                             probe_remote_form_json_submission)


class RemoteFormJSONSubmissionTests(unittest.TestCase):
    def test_schema_fixture_and_false_authority(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_form_json_submission.json').read_text())
        self.assertIs(fixture['synthetic'], True)
        plan = RemoteFormJSONSubmissionPlan.model_validate(fixture['plan'])
        report = RemoteFormJSONSubmissionReport.model_validate(fixture['report'])
        self.assertEqual(report.plan_sha256, digest(plan.model_dump()))
        self.assertEqual(report.oracle_plan_sha256, digest(plan.oracle_plan.model_dump()))
        self.assertEqual(report.submitted_value_sha256, plan.oracle_plan.expected_value_sha256)
        self.assertFalse(report.site_outcome_verified)
        for name, value in (('remote_form_json_submission_plan', plan),
                            ('remote_form_json_submission_report', report)):
            validator(name).validate(value.model_dump())
            self.assertEqual(json.loads((REPO_ROOT / 'schemas' / (name + '.schema.json')).read_text()),
                             type(value).model_json_schema())
        for field in ('execution_authorized', 'collection_authorized'):
            with self.assertRaises(ValueError):
                RemoteFormJSONSubmissionPlan.model_validate({**fixture['plan'], field: True})
        with self.assertRaises(ValueError):
            RemoteFormJSONSubmissionReport.model_validate({
                **fixture['report'], 'submitted_value_sha256': '0' * 64})
        with self.assertRaises(ValueError):
            RemoteFormJSONSubmissionReport.model_validate({
                **fixture['report'], 'site_outcome_verified': True})

    def test_wrong_confirmation_cannot_probe(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_form_json_submission.json').read_text())
        plan = RemoteFormJSONSubmissionPlan.model_validate(fixture['plan'])
        with patch('aos.remote_form_json_submission.plan_remote_form_json_submission') as preview:
            with self.assertRaisesRegex(ValueError, 'exact_confirmation'):
                probe_remote_form_json_submission(
                    Path('/missing.sqlite'), 'missing-run', profiles=Path('/missing-profiles'),
                    plan=plan, confirm_plan_sha256='0' * 64,
                    fields=(('message', 'hello'),))
        preview.assert_not_called()

    def test_cli_private_value_file_never_prints_content(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_form_json_submission.json').read_text())
        plan = RemoteFormJSONSubmissionPlan.model_validate(fixture['plan'])
        report = RemoteFormJSONSubmissionReport.model_validate(fixture['report'])
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            value_file = Path(temporary) / 'value.txt'
            value_file.write_text('hello')
            value_file.chmod(0o600)
            arguments = [
                'submission', '--database', '/missing.sqlite', '--run-id', 'missing-run',
                '--profiles', '/missing-profiles',
                '--profile-sha256', plan.oracle_plan.profile_sha256,
                '--form-plan-sha256', plan.oracle_plan.form_plan_sha256,
                '--url', plan.oracle_plan.url, '--field-key', plan.oracle_plan.field_key,
                '--submitted-field-name', 'message', '--form-value-file', str(value_file),
                '--probe', '--confirm-plan-sha256', digest(plan.model_dump())]
            with (patch.object(sys, 'argv', arguments),
                  patch('aos.remote_form_json_submission.plan_remote_form_json_submission',
                        return_value=plan) as preview,
                  patch('aos.remote_form_json_submission.probe_remote_form_json_submission',
                        return_value=report) as probe,
                  redirect_stdout(StringIO()) as output):
                main()
            self.assertEqual(preview.call_args.kwargs['fields'], (('message', 'hello'),))
            self.assertEqual(probe.call_args.kwargs['fields'], (('message', 'hello'),))
            self.assertNotIn('hello', output.getvalue())
            value_file.chmod(0o644)
            with (patch.object(sys, 'argv', arguments),
                  patch('aos.remote_form_json_submission.probe_remote_form_json_submission') as probe,
                  redirect_stderr(StringIO()) as error,
                  self.assertRaises(SystemExit)):
                main()
            probe.assert_not_called()
            self.assertNotIn('hello', error.getvalue())


if __name__ == '__main__':
    unittest.main()
