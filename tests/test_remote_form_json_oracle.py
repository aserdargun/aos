from contextlib import redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, digest
from aos.dataset import validator
from aos.remote_form_json_oracle import (RemoteFormJSONOraclePlan,
                                         RemoteFormJSONOracleReport,
                                         main,
                                         observed_json_value_sha256,
                                         plan_remote_form_json_oracle,
                                         probe_remote_form_json_oracle)
from aos.storage import TrajectoryStore
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report


class RemoteFormJSONOracleTests(unittest.TestCase):
    def test_cookie_cli_reads_only_private_exact_file(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_form_json_oracle.json').read_text())
        cookie = 'session=synthetic-private'
        cookie_sha256 = hashlib.sha256(cookie.encode()).hexdigest()
        plan = RemoteFormJSONOraclePlan.model_validate({
            **fixture['plan'], 'cookie_sha256': cookie_sha256})
        report = RemoteFormJSONOracleReport.model_validate({
            **fixture['report'], 'plan_sha256': digest(plan.model_dump()),
            'cookie_sha256': cookie_sha256})
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            cookie_file = Path(temporary) / 'cookie.txt'
            cookie_file.write_text(cookie)
            cookie_file.chmod(0o600)
            arguments = [
                'oracle', '--database', '/missing.sqlite', '--run-id', 'missing-run',
                '--profiles', '/missing-profiles', '--profile-sha256', plan.profile_sha256,
                '--form-plan-sha256', plan.form_plan_sha256, '--url', plan.url,
                '--field-key', plan.field_key,
                '--expected-value-sha256', plan.expected_value_sha256,
                '--cookie-sha256', cookie_sha256, '--cookie-file', str(cookie_file),
                '--probe', '--confirm-plan-sha256', digest(plan.model_dump())]
            with (patch.object(sys, 'argv', arguments),
                  patch('aos.remote_form_json_oracle.plan_remote_form_json_oracle', return_value=plan),
                  patch('aos.remote_form_json_oracle.probe_remote_form_json_oracle', return_value=report) as probe,
                  redirect_stdout(StringIO()) as output):
                main()
            self.assertEqual(probe.call_args.kwargs['cookie_header'], cookie)
            self.assertNotIn(cookie, output.getvalue())
            cookie_file.chmod(0o644)
            with (patch.object(sys, 'argv', arguments),
                  patch('aos.remote_form_json_oracle.plan_remote_form_json_oracle', return_value=plan),
                  patch('aos.remote_form_json_oracle.probe_remote_form_json_oracle') as probe,
                  redirect_stderr(StringIO()) as error,
                  self.assertRaises(SystemExit)):
                main()
            probe.assert_not_called()
            self.assertNotIn(cookie, error.getvalue())

    def test_schema_fixture_and_false_authority(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_form_json_oracle.json').read_text())
        self.assertIs(fixture['synthetic'], True)
        plan = RemoteFormJSONOraclePlan.model_validate(fixture['plan'])
        report = RemoteFormJSONOracleReport.model_validate(fixture['report'])
        self.assertEqual(report.plan_sha256, digest(plan.model_dump()))
        self.assertFalse(report.site_outcome_verified)
        self.assertFalse(report.account_verified)
        for name, value in (('remote_form_json_oracle_plan', plan),
                            ('remote_form_json_oracle_report', report)):
            validator(name).validate(value.model_dump())
            self.assertEqual(json.loads((REPO_ROOT / 'schemas' / (name + '.schema.json')).read_text()),
                             type(value).model_json_schema())
        for field in ('execution_authorized', 'collection_authorized'):
            with self.assertRaises(ValueError):
                RemoteFormJSONOraclePlan.model_validate({**fixture['plan'], field: True})
        with self.assertRaises(ValueError):
            RemoteFormJSONOraclePlan.model_validate({**fixture['plan'], 'url': 'http://example.com/oracle'})
        with self.assertRaises(ValueError):
            RemoteFormJSONOraclePlan.model_validate({**fixture['plan'], 'field_key': 'nested.path'})

    def test_bounded_json_scalar_and_duplicate_rejections(self):
        self.assertEqual(observed_json_value_sha256(b'{"status":"saved"}', 'status'),
                         digest({'value': 'saved'}))
        for body in (b'{"status":"a","status":"b"}', b'{"status":{"nested":1}}',
                     b'{"other":"saved"}', b'{"status":NaN}', b'{"status":1e999}',
                     b'not-json', b'[]',
                     b'{"status":"\xff"}'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                observed_json_value_sha256(body, 'status')
        with self.assertRaises(ValueError):
            observed_json_value_sha256(b'{"status":"saved"}', 'bad.path')
        with self.assertRaises(ValueError):
            observed_json_value_sha256(b'{"status":"' + b'a' * 65536 + b'"}', 'status')

    def test_missing_source_or_confirmation_cannot_fetch(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_form_json_oracle.json').read_text())
        plan = RemoteFormJSONOraclePlan.model_validate(fixture['plan'])
        with patch('aos.remote_form_json_oracle._fetch_response') as fetch:
            with self.assertRaisesRegex(ValueError, 'exact_confirmation'):
                probe_remote_form_json_oracle(Path('/missing.sqlite'), 'missing-run',
                                              profiles=Path('/missing-profiles'), plan=plan,
                                              confirm_plan_sha256='0' * 64)
            cookie_plan = plan.model_copy(update={
                'cookie_sha256': hashlib.sha256(b'session=synthetic').hexdigest()})
            with self.assertRaisesRegex(ValueError, 'cookie_scope_mismatch'):
                probe_remote_form_json_oracle(Path('/missing.sqlite'), 'missing-run',
                                              profiles=Path('/missing-profiles'), plan=cookie_plan,
                                              confirm_plan_sha256=digest(cookie_plan.model_dump()))
            with self.assertRaisesRegex(ValueError, 'cookie_hash_mismatch'):
                probe_remote_form_json_oracle(Path('/missing.sqlite'), 'missing-run',
                                              profiles=Path('/missing-profiles'), plan=cookie_plan,
                                              confirm_plan_sha256=digest(cookie_plan.model_dump()),
                                              cookie_header='session=wrong')
        fetch.assert_not_called()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / 'trace.sqlite'
            store = TrajectoryStore(database)
            store.close()
            source = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile']
            profile = WebApplicationProfile.model_validate(source)
            profiles = WebApplicationProfiles(root / 'profiles')
            checksum = profile_report(profile).profile_sha256
            profiles.register(profile, confirm_sha256=checksum)
            with self.assertRaises(ValueError):
                plan_remote_form_json_oracle(
                    database, 'missing-run', profiles=profiles.root,
                    profile_sha256=checksum, form_plan_sha256='b' * 64,
                    url='https://example.com/oracle', field_key='status',
                    expected_value_sha256=digest({'value': 'saved'}))
            command = subprocess.run([
                sys.executable, '-m', 'aos.remote_form_json_oracle',
                '--database', str(database), '--run-id', 'missing-run',
                '--profiles', str(profiles.root), '--profile-sha256', checksum,
                '--form-plan-sha256', 'b' * 64, '--url', 'https://example.com/oracle',
                '--field-key', 'status', '--expected-value-sha256', digest({'value': 'saved'}),
                '--probe', '--confirm-plan-sha256', '0' * 64],
                capture_output=True, text=True, timeout=10)
            self.assertEqual((command.returncode, command.stdout), (1, ''))
            self.assertNotIn(str(database), command.stderr)


if __name__ == '__main__':
    unittest.main()
