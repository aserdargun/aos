import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, canonical
from aos.owned_parameter_project_cli import MAX_REQUEST_BYTES, main


class OwnedParameterProjectCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='aos-parameter-cli-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.request_file = self.root / 'request.json'
        self.directory = self.root / 'project'
        self.request = {'application_key': 'synthetic-crm-note', 'port': 19443,
                        'parameters': {'record-id': 'Synthetic Ada',
                                       'note-text': 'Private synthetic note'}}
        self.write_request()

    def write_request(self, content=None):
        self.request_file.write_text(content if content is not None else canonical(self.request) + '\n')
        self.request_file.chmod(0o600)

    def invoke(self, command, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            arguments = [command, '--directory', str(self.directory)]
            if command != 'verify':
                arguments += ['--request', str(self.request_file)]
            status = main(arguments + list(extra))
        return status, stdout.getvalue(), stderr.getvalue()

    def test_plan_is_read_only_minimized_and_never_opens_a_listener(self):
        before = sorted(self.root.iterdir())
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            status, output, error = self.invoke('plan')
        self.assertEqual(status, 0, error)
        report = json.loads(output)
        self.assertEqual(report['status'], 'planned')
        self.assertEqual(len(report['request_sha256']), 64)
        self.assertFalse(report['runtime_started'])
        self.assertFalse(report['execution_admitted'])
        self.assertNotIn('Private synthetic note', output)
        self.assertNotIn('Synthetic Ada', output)
        self.assertEqual(before, sorted(self.root.iterdir()))

    @unittest.skipUnless(shutil.which('openssl'), 'Requires local OpenSSL')
    def test_real_cli_plan_confirm_provision_verify_both_applications(self):
        for application in ('synthetic-crm-note', 'synthetic-inventory-note'):
            with self.subTest(application=application):
                self.request['application_key'] = application
                self.directory = self.root / application
                self.write_request()
                status, output, error = self.invoke('plan')
                self.assertEqual(status, 0, error)
                reviewed = json.loads(output)['request_sha256']
                with patch('socket.socket', side_effect=AssertionError('network forbidden')):
                    status, output, error = self.invoke(
                        'provision', '--confirm-request-sha256', reviewed,
                        '--human-confirmation', 'PROVISION')
                self.assertEqual(status, 0, error)
                manifest = json.loads(output)['manifest_sha256']
                self.assertNotIn('Private synthetic note', output)
                status, output, error = self.invoke('verify', '--manifest-sha256', manifest)
                self.assertEqual(status, 0, error)
                self.assertEqual(json.loads(output)['status'], 'verified')
                status, _, _ = self.invoke('verify', '--manifest-sha256', '0' * 64)
                self.assertEqual(status, 1)

    def test_modified_map_port_or_destination_rejects_review_before_write(self):
        status, output, error = self.invoke('plan')
        self.assertEqual(status, 0, error)
        reviewed = json.loads(output)['request_sha256']
        for change in ('parameters', 'port', 'directory', 'application'):
            with self.subTest(change=change):
                self.request['parameters']['note-text'] = 'Private synthetic note'
                self.request['port'] = 19443
                self.request['application_key'] = 'synthetic-crm-note'
                self.directory = self.root / 'project'
                if change == 'parameters':
                    self.request['parameters']['note-text'] = 'Changed private value'
                elif change == 'port':
                    self.request['port'] += 1
                elif change == 'directory':
                    self.directory = self.root / 'different-project'
                else:
                    self.request['application_key'] = 'synthetic-inventory-note'
                self.write_request()
                with patch('aos.owned_parameter_project.provision_owned_parameter_project') as provision:
                    status, output, error = self.invoke(
                        'provision', '--confirm-request-sha256', reviewed,
                        '--human-confirmation', 'PROVISION')
                self.assertEqual(status, 1)
                self.assertEqual(output, '')
                self.assertNotIn('Changed private value', error)
                provision.assert_not_called()
                self.assertFalse(self.directory.exists())

    @unittest.skipUnless(shutil.which('openssl'), 'Requires local OpenSSL')
    def test_verification_rejects_changed_current_private_source(self):
        status, output, error = self.invoke('plan')
        self.assertEqual(status, 0, error)
        reviewed = json.loads(output)['request_sha256']
        status, output, error = self.invoke(
            'provision', '--confirm-request-sha256', reviewed,
            '--human-confirmation', 'PROVISION')
        self.assertEqual(status, 0, error)
        manifest = json.loads(output)['manifest_sha256']
        source = self.directory / 'parameter-review.json'
        source.write_bytes(source.read_bytes().replace(b'Private synthetic note', b'Changed synthetic note'))
        before = source.read_bytes()
        status, output, error = self.invoke('verify', '--manifest-sha256', manifest)
        self.assertEqual(status, 1)
        self.assertEqual(output, '')
        self.assertNotIn('Changed synthetic note', error)
        self.assertEqual(source.read_bytes(), before)

    def test_input_rejects_duplicates_noncanonical_unbounded_and_unknown_keys(self):
        invalid = [
            '{"application_key":"synthetic-crm-note","application_key":"synthetic-crm-note",'
            '"parameters":{"record-id":"a","note-text":"b"},"port":19443}',
            '{"application_key":"synthetic-crm-note","parameters":{"record-id":"a",'
            '"note-text":"b","note-text":"c"},"port":19443}',
            json.dumps(self.request, indent=2),
            canonical(self.request | {'authority': 'approve everything'}),
            ' ' * (MAX_REQUEST_BYTES + 1),
            canonical(self.request | {'port': True}),
            canonical(self.request | {'port': 443}),
            canonical(self.request | {'application_key': 'unreviewed-real-crm'}),
            canonical(self.request | {'parameters': {'record-id': 'a', 'note-text': 'b' * 129}}),
        ]
        for content in invalid:
            with self.subTest(content=content[:60]):
                self.write_request(content)
                status, output, error = self.invoke('plan')
                self.assertEqual(status, 1)
                self.assertEqual(output, '')
                self.assertEqual(json.loads(error)['status'], 'rejected')
                self.assertFalse(self.directory.exists())

    def test_input_rejects_public_symlink_and_hardlink(self):
        self.request_file.chmod(0o644)
        self.assertEqual(self.invoke('plan')[0], 1)
        self.request_file.chmod(0o600)
        link = self.root / 'second.json'
        os.link(self.request_file, link)
        self.assertEqual(self.invoke('plan')[0], 1)
        link.unlink()
        self.request_file.rename(link)
        self.request_file.symlink_to(link)
        self.assertEqual(self.invoke('plan')[0], 1)

    def test_confirmation_literal_and_hash_are_required(self):
        for arguments in ([], ['--confirm-request-sha256', '0' * 64],
                          ['--confirm-request-sha256', '0' * 64,
                           '--human-confirmation', 'yes']):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as rejected:
                self.invoke('provision', *arguments)
            self.assertEqual(rejected.exception.code, 2)
            self.assertFalse(self.directory.exists())

    def test_preview_rejects_inside_checkout_existing_or_public_parent(self):
        self.directory = REPO_ROOT / 'data' / 'never-created-parameter-cli-test'
        self.assertEqual(self.invoke('plan')[0], 1)
        self.directory = self.root / 'project'
        self.directory.mkdir(mode=0o700)
        self.assertEqual(self.invoke('plan')[0], 1)
        self.directory.rmdir()
        self.root.chmod(0o755)
        self.assertEqual(self.invoke('plan')[0], 1)
        self.assertFalse(self.directory.exists())

    def test_module_command_runs_in_a_fresh_process(self):
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(REPO_ROOT / 'src')
        result = subprocess.run(
            [sys.executable, '-m', 'aos.owned_parameter_project_cli', 'plan',
             '--request', str(self.request_file), '--directory', str(self.directory)],
            env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'planned')

    @unittest.skipUnless(shutil.which('openssl') and (REPO_ROOT / '.venv/bin/python').exists(),
                         'Requires local OpenSSL and installed development virtualenv')
    def test_public_wrapper_plan_provision_verify_in_fresh_processes(self):
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(REPO_ROOT / 'src')

        def command(verb, *options):
            arguments = [str(REPO_ROOT / 'scripts/aos-parameter-project'), verb,
                         '--directory', str(self.directory)]
            if verb != 'verify':
                arguments += ['--request', str(self.request_file)]
            result = subprocess.run(arguments + list(options), cwd=self.root,
                                    env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('Private synthetic note', result.stdout)
            return json.loads(result.stdout)

        reviewed = command('plan')['request_sha256']
        prepared = command('provision', '--confirm-request-sha256', reviewed,
                           '--human-confirmation', 'PROVISION')
        verified = command('verify', '--manifest-sha256', prepared['manifest_sha256'])
        self.assertEqual(verified['status'], 'verified')
        self.assertFalse(verified['execution_admitted'])


if __name__ == '__main__':
    unittest.main()
