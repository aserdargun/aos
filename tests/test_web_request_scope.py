import copy
import json
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
from aos.web_application_binding import WebRuntimePin, WebTaskContract, bind_web_task
from aos.web_request_scope import require_request_origin


FIXTURE = json.loads((REPO_ROOT / 'examples/web_application_binding.json').read_text())
PROFILE = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile']


class WebRequestScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.profiles = WebApplicationProfiles(Path(temporary.name) / 'profiles')
        profile = WebApplicationProfile.model_validate(copy.deepcopy(PROFILE))
        self.profiles.register(profile, confirm_sha256=profile_report(profile).profile_sha256)
        task = WebTaskContract.model_validate(copy.deepcopy(FIXTURE['task']))
        runtime = WebRuntimePin.model_validate(copy.deepcopy(FIXTURE['runtime']))
        self.draft = bind_web_task(self.profiles, task, runtime)

    def test_exact_origin_matches_navigation_redirect_and_subresource_urls(self):
        for url in ('https://crm.example.invalid/',
                    'https://crm.example.invalid/records?page=2',
                    'https://crm.example.invalid/assets/app%20v2.js?build=17'):
            with self.subTest(url=url):
                self.assertEqual(require_request_origin(self.profiles, self.draft, url),
                                 'https://crm.example.invalid')

    def test_cross_origin_and_ambiguous_urls_fail_closed(self):
        for url in ('https://other.example.invalid/', 'https://crm.example.invalid.evil.invalid/',
                    'https://crm.example.invalid:444/', 'http://crm.example.invalid/',
                    'https://CRM.example.invalid/', 'https://crm.example.invalid:443/',
                    'https://crm.example.invalid@other.example.invalid/',
                    'https://crm.example.invalid\\@other.example.invalid/',
                    'https://crm.example.invalid/#fragment',
                    'https://crm.example.invalid/\nmore',
                    'file:///etc/passwd', 'data:text/html,hello', 'blob:https://crm.example.invalid/id',
                    '/relative', 'https://crm.example.invalid',
                    'https://crm.example.invalid/' + 'a' * 8192):
            with self.subTest(url=url), self.assertRaises(ValueError):
                require_request_origin(self.profiles, self.draft, url)

    def test_mutated_task_scope_is_revalidated(self):
        self.draft.task.allowed_origins.append('https://other.example.invalid')
        with self.assertRaises(ValueError):
            require_request_origin(self.profiles, self.draft, 'https://other.example.invalid/')


if __name__ == '__main__':
    unittest.main()
