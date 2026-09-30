import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import canonical, digest
from aos.site_skill_form_recipe_candidate import (
    SiteSkillFormRecipeCandidate,
    SiteSkillFormRecipeCandidateAnnotation,
    OwnedSiteSkillFormRecipeCandidate,
    _main,
    _read_candidate,
    persist_site_skill_form_recipe_candidate,
    parse_site_skill_form_recipe_candidate,
)


class SiteSkillFormRecipeCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(
            (Path(__file__).resolve().parents[1]
             / 'examples/site_skill_form_recipe_candidate.json').read_text())
        cls.value = cls.fixture['candidate']
        cls.candidate = SiteSkillFormRecipeCandidate.model_validate_json(
            canonical(cls.value))

    def test_example_is_synthetic_unreviewed_and_ordered(self):
        self.assertIs(self.fixture['synthetic'], True)
        self.assertEqual(self.candidate.skill.source_kind, 'manual_candidate')
        self.assertEqual(len(self.candidate.source_stage_refs), 6)
        self.assertEqual(self.candidate.source_event_ids_by_role['system2'], [])
        self.assertEqual(
            [step.operation for step in self.candidate.recipe.steps],
            ['open_entry', 'read_state_before', 'fill_form', 'submit_form',
             'read_receipt', 'read_state_after'])
        self.assertIs(self.candidate.reviewed, False)
        self.assertIs(self.candidate.activation_authorized, False)
        self.assertIs(self.candidate.training_ready, False)
        self.assertIs(self.candidate.skill_executed, False)
        self.assertNotIn('alpha', canonical(self.value))

    def test_claims_and_lineage_are_strict(self):
        for claim in ('reviewed', 'activation_authorized', 'training_ready',
                      'site_outcome_verified', 'skill_executed'):
            changed = dict(self.value, **{claim: 0})
            with self.subTest(claim=claim), self.assertRaises(ValueError):
                SiteSkillFormRecipeCandidate.model_validate_json(canonical(changed))
        changed = dict(self.value, source_group_sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'source_group'):
            SiteSkillFormRecipeCandidate.model_validate_json(canonical(changed))

    def test_annotation_requires_explicit_unique_supported_mapping(self):
        annotation = self.value['annotation']
        changed = json.loads(canonical(annotation))
        changed['operation_step_keys'][0]['step_key'] = changed['operation_step_keys'][1]['step_key']
        with self.assertRaisesRegex(ValueError, 'mapping_invalid'):
            SiteSkillFormRecipeCandidateAnnotation.model_validate_json(canonical(changed))

    def test_owned_source_context_is_a_distinct_backward_compatible_version(self):
        owned = dict(self.value, schema_version='1.1', source_context={
            'kind': 'owned_fixed_template_v1', 'manifest_sha256': 'a' * 64,
            'invocation_sha256': 'b' * 64})
        parsed = parse_site_skill_form_recipe_candidate(owned)
        self.assertIsInstance(parsed, OwnedSiteSkillFormRecipeCandidate)
        self.assertEqual(parsed.source_context.kind, 'owned_fixed_template_v1')
        with self.assertRaises(ValueError):
            parse_site_skill_form_recipe_candidate(dict(self.value, schema_version='1.1'))
        with self.assertRaises(ValueError):
            parse_site_skill_form_recipe_candidate(dict(owned, source_context={
                **owned['source_context'], 'raw_directory': '/tmp/private'}))
        fixture = json.loads((Path(__file__).resolve().parents[1]
                              / 'examples/site_skill_form_recipe_candidate_owned.json').read_text())
        checked_fixture = parse_site_skill_form_recipe_candidate(fixture['candidate'])
        self.assertEqual(checked_fixture.schema_version, '1.1')
        self.assertEqual(checked_fixture.source_context.kind, 'owned_fixed_template_v1')
        self.assertNotIn('source_parameters', fixture['candidate'])

    def test_private_content_addressed_store_requires_exact_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'candidates'
            checksum = digest(self.value)
            with self.assertRaisesRegex(ValueError, 'confirmation'):
                persist_site_skill_form_recipe_candidate(
                    directory, self.value, confirm_sha256='0' * 64)
            self.assertEqual(persist_site_skill_form_recipe_candidate(
                directory, self.value, confirm_sha256=checksum), checksum)
            self.assertEqual(persist_site_skill_form_recipe_candidate(
                directory, self.value, confirm_sha256=checksum), checksum)
            stored = directory / (checksum + '.json')
            self.assertEqual(os.stat(stored).st_mode & 0o777, 0o600)
            self.assertEqual(_read_candidate(directory, checksum), self.candidate)
            tampered = stored.read_bytes().replace(b'"reviewed":false', b'"reviewed": true')
            stored.write_bytes(tampered)
            with self.assertRaises(ValueError):
                _read_candidate(directory, checksum)
            stored.write_bytes(b'{')
            with self.assertRaises(ValueError):
                _read_candidate(directory, checksum)

    def test_private_store_rejects_linked_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'candidates'
            checksum = digest(self.value)
            persist_site_skill_form_recipe_candidate(
                directory, self.value, confirm_sha256=checksum)
            source = directory / (checksum + '.json')
            linked = directory / 'linked.json'
            os.link(source, linked)
            with self.assertRaisesRegex(ValueError, 'invalid_private'):
                _read_candidate(directory, checksum)
            with self.assertRaises(ValueError):
                persist_site_skill_form_recipe_candidate(
                    directory, self.value, confirm_sha256=checksum)
            linked.unlink()
            directory_alias = Path(temporary) / 'directory-alias'
            directory_alias.symlink_to(directory, target_is_directory=True)
            with self.assertRaises(OSError):
                _read_candidate(directory_alias, checksum)

    def test_cli_preview_publish_and_inspect_emit_only_bounded_summary(self):
        candidate = self.value
        summary = {'schema_version': '1.0', 'candidate_sha256': digest(candidate),
                   'status': candidate['status'], 'reviewed': False,
                   'activation_authorized': False, 'training_ready': False,
                   'site_outcome_verified': False, 'skill_executed': False}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parameters = root / 'parameters.json'
            parameters.write_bytes(b'{"record-query":"alpha"}')
            parameters.chmod(0o600)
            annotation = root / 'annotation.json'
            annotation.write_bytes(canonical(candidate['annotation']).encode())
            annotation.chmod(0o600)
            shared = ['--database', str(root / 'unused.sqlite'), '--profiles',
                      str(root / 'profiles'), '--pages', str(root / 'pages'),
                      '--source-parameters-file', str(parameters)]
            output = []
            with (patch('aos.local_app.private_read', side_effect=lambda path, _limit: Path(path).read_bytes()),
                  patch('aos.site_skill_form_recipe_candidate.WebApplicationProfiles'),
                  patch('aos.site_skill_form_recipe_candidate.SiteKnowledgeStore'),
                  patch('aos.site_skill_form_recipe_candidate.derive_site_skill_form_recipe_candidate',
                        return_value=candidate),
                  patch('aos.site_skill_form_recipe_candidate.persist_site_skill_form_recipe_candidate') as persist,
                  patch('aos.site_skill_form_recipe_candidate.load_site_skill_form_recipe_candidate',
                        return_value=candidate),
                  patch('builtins.print', side_effect=output.append)):
                _main(['preview', *shared, '--run-id', 'synthetic-run',
                       '--annotation-file', str(annotation)])
                self.assertEqual(json.loads(output.pop()),
                                 {**summary, 'source_group_sha256': candidate['source_group_sha256'],
                                  'source_fingerprint_sha256': candidate['source_fingerprint_sha256'],
                                  'source_parameter_variant_sha256': candidate['source_parameter_variant_sha256'],
                                  'skill_sha256': digest(candidate['skill']),
                                  'recipe_sha256': digest(candidate['recipe']),
                                  'steps': candidate['recipe']['steps']})
                directory = root / 'published'
                _main(['publish', *shared, '--run-id', 'synthetic-run',
                       '--annotation-file', str(annotation), '--directory', str(directory),
                       '--confirm-sha256', digest(candidate)])
                persist.assert_called_once()
                _main(['inspect', *shared, '--directory', str(directory), '--sha256', digest(candidate)])
                self.assertEqual(len(output), 2)

    def test_cli_owned_publish_and_inspect_need_no_raw_parameter_file(self):
        owned = dict(self.value, schema_version='1.1', source_context={
            'kind': 'owned_fixed_template_v1', 'manifest_sha256': 'a' * 64,
            'invocation_sha256': 'b' * 64})
        checksum = digest(owned)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'owned-form'
            candidate_directory = source / 'site-skill-recipe-candidates'
            annotation = root / 'annotation.json'
            annotation.write_bytes(canonical(owned['annotation']).encode())
            annotation.chmod(0o600)
            common = ['--database', str(root / 'trajectory.sqlite'), '--profiles',
                      str(source / 'profiles'), '--pages', str(source / 'site-knowledge'),
                      '--owned-source-directory', str(source),
                      '--owned-source-manifest-sha256', 'a' * 64]
            output = []
            with (patch('aos.local_app.private_read', side_effect=lambda path, _limit: Path(path).read_bytes()),
                  patch('aos.site_skill_form_recipe_candidate.WebApplicationProfiles'),
                  patch('aos.site_skill_form_recipe_candidate.SiteKnowledgeStore'),
                  patch('aos.site_skill_form_recipe_candidate.derive_site_skill_form_recipe_candidate',
                        return_value=owned) as derive,
                  patch('aos.site_skill_form_recipe_candidate.persist_site_skill_form_recipe_candidate'),
                  patch('aos.site_skill_form_recipe_candidate.load_site_skill_form_recipe_candidate',
                        return_value=owned) as load,
                  patch('builtins.print', side_effect=output.append)):
                _main(['publish', *common, '--run-id', 'synthetic-run',
                       '--annotation-file', str(annotation), '--directory', str(candidate_directory),
                       '--confirm-sha256', checksum])
                self.assertEqual(derive.call_args.kwargs['source_parameters'], None)
                self.assertEqual(derive.call_args.kwargs['owned_source_manifest_sha256'], 'a' * 64)
                _main(['inspect', *common, '--directory', str(candidate_directory),
                       '--sha256', checksum])
                self.assertEqual(load.call_args.kwargs['source_parameters'], None)
                self.assertEqual(load.call_args.kwargs['owned_source_directory'], source)
                self.assertEqual(len(output), 2)


if __name__ == '__main__':
    unittest.main()
