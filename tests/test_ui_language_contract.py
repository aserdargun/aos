import ast
import re
import unittest

from aos.contracts import REPO_ROOT


LITERAL = r'''(?:'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")'''


class LanguageContractTests(unittest.TestCase):
    def test_static_translation_keys_have_unambiguous_english_values(self):
        directory = REPO_ROOT / 'ui/src'
        translations = {}
        for filename in ('translations_core.ts', 'translations_tasks.ts'):
            source = (directory / filename).read_text()
            for match in re.finditer(f'({LITERAL})\\s*:\\s*({LITERAL})', source):
                key, value = (ast.literal_eval(item) for item in match.groups())
                self.assertTrue(value.strip(), key)
                if key in translations:
                    self.assertEqual(translations[key], value, 'Conflicting translation: ' + key)
                translations[key] = value
        self.assertGreater(len(translations), 20)
        missing = []
        for path in directory.glob('*.ts*'):
            if path.name.startswith('translations_'):
                continue
            for match in re.finditer(r'\bt\(\s*(' + LITERAL + ')', path.read_text()):
                key = ast.literal_eval(match.group(1))
                if key not in translations:
                    missing.append(path.name + ': ' + key)
        self.assertEqual(missing, [], 'Missing English translations')
