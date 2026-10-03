from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.publish_usage import FILES, PRICES, REPORT, git, hourly_snapshot, public_snapshot, publish
from scripts.record_usage import FIELDS
from scripts.summarize_usage import render


class UsagePublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.counts = dict(zip(FIELDS, [100, 80, 0, 10, 5, 110]))
        self.prices = {'schema': 'aos.api-price-reference.v1', 'usd_per_million_tokens': {
            'gpt-6.1-sol': {'input': '2', 'cached_input': '0.1', 'output': '10'}}}
        self.snapshot = {'schema_version': '1', 'status': 'observed', 'cutoff': '2026-10-03T18:00:00Z',
            'scope': 'available_AOS_Codex_session_usage_records',
            'coverage': {'sessions': 1, 'first_usage_event': '2026-10-03T17:00:00Z',
                         'last_usage_event': '2026-10-03T17:59:00Z'},
            'counts': self.counts, 'models': [{'provider': 'openai', 'model': 'gpt-6.1-sol',
                'effort': 'high', 'counts': self.counts}],
            'days': [{'day': '2026-10-03', 'counts': self.counts}]}

    def initialize_repositories(self):
        self.remote = self.root / 'remote.git'
        self.repository = self.root / 'publisher.git'
        source = self.root / 'source'
        for directory in (self.remote, self.repository):
            subprocess.run(['git', 'init', '--bare', '--initial-branch=main', str(directory)],
                           check=True, capture_output=True)
            directory.chmod(0o700)
        subprocess.run(['git', 'init', '--initial-branch=main', str(source)], check=True, capture_output=True)
        for repository in (source / '.git', self.repository):
            git(repository, 'config', 'user.name', 'Synthetic accounting test')
            git(repository, 'config', 'user.email', 'synthetic@example.invalid')
        content = {'README.md': ('Unrelated documentation\n<!-- aos-usage:start -->\n'
                    + render(self.snapshot, self.prices) + '<!-- aos-usage:end -->\nKeep this.\n').encode(),
                   REPORT: json.dumps(public_snapshot(self.snapshot, self.prices)).encode(),
                   PRICES: json.dumps(self.prices).encode(), 'application.py': b'unchanged = True\n'}
        for name, value in content.items():
            destination = source / name
            destination.parent.mkdir(exist_ok=True, parents=True)
            destination.write_bytes(value)
        manifest = ''.join(hashlib.sha256(content[name]).hexdigest() + '  ' + name + '\n' for name in sorted(content))
        (source / 'MANIFEST.sha256').write_text(manifest)
        subprocess.run(['git', '-C', str(source), 'add', '.'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(source), 'commit', '-m', 'Synthetic baseline'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(source), 'push', str(self.remote), 'main'], check=True, capture_output=True)
        git(self.repository, 'remote', 'add', 'origin', str(self.remote))
        self.price_hash = hashlib.sha256(content[PRICES]).hexdigest()
        self.snapshot['cutoff'] = '2026-10-03T19:00:00Z'
        self.counts.update(input_tokens=200, total_tokens=210)

    def execute(self, **overrides):
        arguments = {'remote': str(self.remote), 'branch': 'main', 'price_sha256': self.price_hash, 'perform': True}
        arguments.update(overrides)
        return publish(self.repository, self.snapshot, **arguments)

    def test_only_allowlisted_aggregate_files_publish_and_retry_is_noop(self):
        self.initialize_repositories()
        self.assertFalse(self.execute(perform=False)['pushed'])
        self.snapshot['private_message'] = 'PRIVATE_SENTINEL'
        self.snapshot['coverage']['session_id'] = 'PRIVATE_SENTINEL'
        result = self.execute()
        self.assertTrue(result['pushed'])
        self.assertEqual(set(result['files']), FILES)
        for filename in FILES:
            content = git(self.remote, 'show', 'main:' + filename)
            self.assertNotIn(b'PRIVATE_SENTINEL', content)
        readme = git(self.remote, 'show', 'main:README.md').decode()
        self.assertTrue(readme.startswith('Unrelated documentation\n'))
        self.assertTrue(readme.endswith('Keep this.\n'))
        self.assertEqual(git(self.remote, 'show', 'main:application.py'), b'unchanged = True\n')
        self.assertEqual(self.execute()['status'], 'unchanged')
        self.snapshot['cutoff'] = '2026-10-03T20:00:00Z'
        self.assertEqual(self.execute()['status'], 'unchanged')
        self.assertEqual(git(self.remote, 'rev-parse', 'main').decode().strip(), result['commit'])

    def test_remote_price_and_counter_changes_fail_closed(self):
        self.initialize_repositories()
        for options in ({'remote': 'https://example.invalid/wrong.git'}, {'price_sha256': '0' * 64}):
            with self.assertRaises(ValueError):
                self.execute(**options)
        self.snapshot['cutoff'] = '2026-10-03T17:00:00Z'
        with self.assertRaises(ValueError):
            self.execute()
        self.snapshot['cutoff'] = '2026-10-03T19:00:00Z'
        self.counts.update(input_tokens=90, total_tokens=100)
        with self.assertRaises(ValueError):
            self.execute()

    def test_concurrent_remote_update_rejects_push_without_force(self):
        self.initialize_repositories()
        remote_update = []

        def concurrent_git(repository, *arguments, **keywords):
            if arguments[0] == 'push':
                parent = git(self.remote, 'rev-parse', 'main').decode().strip()
                tree = git(self.remote, 'rev-parse', 'main^{tree}').decode().strip()
                git(self.remote, 'config', 'user.name', 'Synthetic peer')
                git(self.remote, 'config', 'user.email', 'peer@example.invalid')
                commit = git(self.remote, 'commit-tree', tree, '-p', parent, data=b'Synthetic concurrent change\n').decode().strip()
                git(self.remote, 'update-ref', 'refs/heads/main', commit, parent)
                remote_update.append(commit)
                self.assertFalse(any('force' in argument or argument.startswith('+') for argument in arguments))
            return git(repository, *arguments, **keywords)

        with patch('scripts.publish_usage.git', side_effect=concurrent_git):
            with self.assertRaisesRegex(ValueError, 'Git operation failed: push'):
                self.execute()
        self.assertEqual(git(self.remote, 'rev-parse', 'main').decode().strip(), remote_update[0])

    def test_projection_drops_private_data_at_every_nesting_level(self):
        snapshot = deepcopy(self.snapshot)
        snapshot['models'][0]['counts']['message'] = 'PRIVATE_SENTINEL'
        snapshot['models'][0]['session_id'] = 'PRIVATE_SENTINEL'
        snapshot['days'][0]['path'] = 'PRIVATE_SENTINEL'
        snapshot['runtime'] = {'credentials': 'PRIVATE_SENTINEL'}
        result = public_snapshot(snapshot, self.prices)
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))
        self.assertIsNone(result['invoice_cost'])
        self.assertIsNone(result['subscription'])
        self.assertFalse(result['goal_counters_included'])
        snapshot['models'][0]['model'] = 'PRIVATE_SENTINEL'
        with self.assertRaises(ValueError):
            public_snapshot(snapshot, self.prices)

    def test_missing_stale_future_and_symlinked_hourly_snapshots_rejected(self):
        now = datetime(2026, 10, 3, 19, 5, tzinfo=timezone.utc)
        with self.assertRaises(OSError):
            hourly_snapshot(self.root, now)
        path = self.root / '2026-10-03T190000Z.json'
        path.write_text(json.dumps(self.snapshot))
        with self.assertRaises(ValueError):
            hourly_snapshot(self.root, now)
        self.snapshot['cutoff'] = '2026-10-03T19:06:00Z'
        path.write_text(json.dumps(self.snapshot))
        with self.assertRaises(ValueError):
            hourly_snapshot(self.root, now)
        self.snapshot['cutoff'] = '2026-10-03T19:00:00Z'
        path.write_text(json.dumps(self.snapshot))
        self.assertEqual(hourly_snapshot(self.root, now)['cutoff'], self.snapshot['cutoff'])
        path.unlink()
        path.symlink_to(self.root / 'missing')
        with self.assertRaises(OSError):
            hourly_snapshot(self.root, now)


if __name__ == '__main__':
    unittest.main()
