import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

from aos.contracts import REPO_ROOT, canonical, digest
from aos.owned_form_fixture import OwnedFormFixture
from aos.owned_form_invocation_session import verify_owned_form_invocation_manifest
from aos.owned_parameter_project import (
    APPLICATIONS, MANIFEST_NAME, OwnedParameterProjectManifest,
    owned_parameter_project_review, owned_parameter_project_review_sha256,
    provision_owned_parameter_project, read_owned_parameter_project,
    verify_owned_parameter_project_manifest,
)
from aos.web_https_form_state_probe import ExactHTTPSFormStateProbe, form_state_request_sha256
from aos.web_https_form_transport import ExactHTTPSFormTransport


class OwnedParameterProjectReviewTests(unittest.TestCase):
    def test_review_is_exact_scoped_bounded_and_does_not_create_sources(self):
        parameters = {'record-id': 'Ada', 'note-text': 'Call tomorrow'}
        first = owned_parameter_project_review(APPLICATIONS[0], parameters)
        second = owned_parameter_project_review(APPLICATIONS[1], parameters)
        self.assertNotEqual(digest(first), digest(second))
        self.assertEqual(owned_parameter_project_review_sha256(APPLICATIONS[0], parameters), digest(first))
        self.assertFalse(first['execution_authorized'] or first['training_ready'])
        for application, values in (
                ('crm', parameters), (APPLICATIONS[0], {'record-id': 'Ada'}),
                (APPLICATIONS[0], parameters | {'extra': 'unauthorized'}),
                (APPLICATIONS[0], parameters | {'record-id': ''}),
                (APPLICATIONS[0], parameters | {'note-text': 'x' * 129}),
                (APPLICATIONS[0], parameters | {'note-text': '🙂' * 65}),
                (APPLICATIONS[0], parameters | {'note-text': 'secret\nnewline'}),
                (APPLICATIONS[0], parameters | {'record-id': 1})):
            with self.subTest(application=application, values=values), self.assertRaises(ValueError):
                owned_parameter_project_review(application, values)

    def test_review_denial_and_invalid_port_have_no_write_side_effect(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'denied'
            parameters = {'record-id': 'Ada', 'note-text': 'Call tomorrow'}
            arguments = dict(application_key=APPLICATIONS[0], parameters=parameters,
                             confirm_parameters_sha256=owned_parameter_project_review_sha256(
                                 APPLICATIONS[0], parameters), human_confirmation=True)
            for changes in ({'human_confirmation': False}, {'human_confirmation': 1},
                            {'confirm_parameters_sha256': '0' * 64},
                            {'parameters': parameters | {'note-text': 'Different'}}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    provision_owned_parameter_project(directory, 19443, **(arguments | changes))
                self.assertFalse(directory.exists())
            for port in (True, 0, 443, 65536, '19443'):
                with self.subTest(port=port), self.assertRaises(ValueError):
                    provision_owned_parameter_project(directory, port, **arguments)
                self.assertFalse(directory.exists())


@unittest.skipUnless(shutil.which('openssl'), 'Requires local OpenSSL for private synthetic TLS')
class OwnedParameterProjectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='aos-parameter-project-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def provision(self, name='project', application=APPLICATIONS[0], parameters=None, port=19443):
        parameters = parameters or {'record-id': 'Ada', 'note-text': 'Call tomorrow'}
        return provision_owned_parameter_project(
            self.root / name, port, application_key=application, parameters=parameters,
            confirm_parameters_sha256=owned_parameter_project_review_sha256(application, parameters),
            human_confirmation=True)

    def verify(self, bundle):
        return verify_owned_parameter_project_manifest(bundle['directory'], bundle['manifest_sha256'])

    def test_two_apps_four_maps_recompile_canonical_private_sources(self):
        bundles = []
        for index, (application, record_id, note) in enumerate((
                (APPLICATIONS[0], 'Ada', 'Call tomorrow'),
                (APPLICATIONS[0], 'Grace', 'Send brief'),
                (APPLICATIONS[1], 'SKU-42', 'Inspect stock'),
                (APPLICATIONS[1], 'SKU-73', 'Check shelf'))):
            bundle = self.provision(str(index), application, {'record-id': record_id, 'note-text': note})
            bundles.append(bundle)
            loaded = read_owned_parameter_project(bundle['directory'], bundle['manifest_sha256'])
            self.assertEqual(loaded['invocation'], bundle['invocation'])
            self.assertEqual(len(loaded['invocation']['steps']), 6)
            self.assertEqual(loaded['fields'], bundle['fields'])
            self.assertEqual(loaded['record_config']['scope']['application_id'], application)
            self.assertEqual(self.verify(bundle), bundle['manifest'])
            self.assertEqual(bundle['directory'].stat().st_mode & 0o777, 0o700)
            for name, pin in bundle['source_pins'].items():
                path = bundle['directory'] / name
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), pin)
            claims = [key for key in bundle['manifest'] if key.endswith('_verified')
                      or key.endswith('_authorized') or key in {'execution_performed', 'skill_reviewed', 'training_ready'}]
            self.assertTrue(all(bundle['manifest'][key] is False for key in claims))
            skill = loaded['skill_store'].get(loaded['skill_sha256'])
            authoring = json.loads((bundle['directory'] / 'synthetic-authoring-source.json').read_text())
            self.assertEqual(skill.source_event_ids, ['learning-' + digest(authoring)])
            self.assertEqual(skill.source_verification_ids, [])
        self.assertNotEqual(bundles[0]['profile_sha256'], bundles[2]['profile_sha256'])
        self.assertEqual(len({item['parameters_review_sha256'] for item in bundles}), 4)
        self.assertEqual(set(dict(bundles[0]['fields'])), {'contact_name', 'note'})
        self.assertEqual(set(dict(bundles[2]['fields'])), {'item_code', 'note'})

    def test_manifest_schema_matches_model_and_does_not_contain_private_values(self):
        bundle = self.provision(parameters={'record-id': 'Private synthetic id',
                                            'note-text': 'Private synthetic note'})
        schema = json.loads((REPO_ROOT / 'schemas/owned_parameter_project_manifest.schema.json').read_text())
        self.assertEqual(schema, OwnedParameterProjectManifest.model_json_schema())
        jsonschema.Draft202012Validator(schema).validate(bundle['manifest'])
        self.assertNotIn('Private synthetic', canonical(self.verify(bundle)))
        for field in ('source_evidence_verified', 'execution_authorized', 'training_ready'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                OwnedParameterProjectManifest.model_validate(bundle['manifest'] | {field: True})
        with self.assertRaises((ValueError, OSError)):
            verify_owned_form_invocation_manifest(bundle['directory'], bundle['manifest_sha256'])

    def test_symlink_hardlink_permissions_and_same_content_replacement_reject(self):
        bundle = self.provision()
        path = bundle['directory'] / 'owned-record-config.json'
        content = path.read_bytes()
        saved = self.root / 'saved.json'
        path.rename(saved)
        path.symlink_to(saved)
        with self.assertRaises((ValueError, OSError)):
            self.verify(bundle)
        path.unlink()
        saved.rename(path)
        os.link(path, self.root / 'hardlink.json')
        with self.assertRaises(ValueError):
            self.verify(bundle)
        (self.root / 'hardlink.json').unlink()
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.verify(bundle)
        path.chmod(0o600)
        path.rename(saved)
        path.write_bytes(content)
        path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'source_identity_changed'):
            self.verify(bundle)

    def test_changed_source_registry_and_manifest_pins_reject(self):
        bundle = self.provision()
        for name in ('remote-form-skill-case-inputs.json', 'remote-form-skill-recipe.json',
                     'synthetic-authoring-source.json', 'profiles/' + bundle['profile_sha256'] + '.json',
                     MANIFEST_NAME):
            with self.subTest(name=name):
                path = bundle['directory'] / name
                content = path.read_bytes()
                path.write_bytes(content + b' ')
                with self.assertRaises(ValueError):
                    self.verify(bundle)
                path.write_bytes(content)
        self.assertEqual(self.verify(bundle), bundle['manifest'])

    def test_source_changes_during_compilation_cannot_return_verified_context(self):
        bundle = self.provision()
        from aos.owned_parameter_project import compile_site_skill_form_recipe_invocation

        def changed(*arguments):
            result = compile_site_skill_form_recipe_invocation(*arguments)
            path = bundle['directory'] / 'parameter-review.json'
            path.write_bytes(path.read_bytes() + b' ')
            return result

        with patch('aos.owned_parameter_project.compile_site_skill_form_recipe_invocation', changed):
            with self.assertRaises(ValueError):
                self.verify(bundle)

    def test_existing_target_and_unsafe_parent_never_overwrite(self):
        bundle = self.provision()
        with self.assertRaises(FileExistsError):
            self.provision()
        self.assertEqual(self.verify(bundle), bundle['manifest'])
        parent = self.root / 'unsafe'
        parent.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            self.provision('unsafe/project')
        parent.chmod(0o700)
        alias = self.root / 'alias'
        alias.symlink_to(parent, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            self.provision('alias/project')
        self.assertFalse((parent / 'project').exists())

    def test_host_sources_drive_real_owned_tls_transport_and_all_field_readback(self):
        for index, application in enumerate(APPLICATIONS):
            with self.subTest(application=application):
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.addCleanup(listener.close)
                listener.bind(('127.0.0.1', 0))
                listener.listen(8)
                bundle = self.provision('tls-' + str(index), application,
                                        {'record-id': 'Synthetic <&> id', 'note-text': 'Synthetic <&> note'},
                                        listener.getsockname()[1])
                plan, state_plan = bundle['form_plan'], bundle['state_plan']
                fixture = OwnedFormFixture(
                    listener.fileno(), origin=bundle['manifest']['origin'],
                    profile_sha256=bundle['profile_sha256'], form_plan_sha256=bundle['form_plan_sha256'],
                    state_plan_sha256=bundle['state_plan_sha256'], entry_url=plan.entry_url,
                    submit_url=plan.submit_url, receipt_url=plan.receipt_url, state_url=state_plan.state_url,
                    expected_body=bundle['body'], certificate_file=bundle['certificate_file'],
                    key_file=bundle['key_file'], certificate_sha256=bundle['certificate_sha256'],
                    record_config=bundle['record_config'])
                self.addCleanup(fixture.close)
                transport = ExactHTTPSFormTransport(
                    bundle['profiles'], bundle['task'], plan, bundle['form_plan_sha256'],
                    consume_approval=lambda _request: True, owned_form_target=fixture.target)
                probe = ExactHTTPSFormStateProbe(
                    transport, state_plan, bundle['state_plan_sha256'], consume_approval=lambda _request: True)
                probe.bind_submitted_fields(bundle['fields'])
                self.assertEqual(transport.open_entry(confirm_request_sha256=digest(
                    {'method': 'GET', 'url': plan.entry_url})), bundle['bodies']['entry'])
                probe.observe_before(confirm_request_sha256=form_state_request_sha256(state_plan, 'before'))
                transport.submit(bundle['body'], confirm_request_sha256=digest(
                    {'method': 'POST', 'url': plan.submit_url, 'body_sha256': plan.body_sha256}))
                transport.read_receipt(confirm_request_sha256=digest({'method': 'GET', 'url': plan.receipt_url}))
                probe.observe_after(confirm_request_sha256=form_state_request_sha256(state_plan, 'after'))
                fixture.verify_complete()
                observed = json.loads(fixture.read_whole_record(
                    profile_sha256=bundle['profile_sha256'], form_plan_sha256=bundle['form_plan_sha256'],
                    state_plan_sha256=bundle['state_plan_sha256']))
                self.assertEqual(observed['record'], dict(bundle['fields']))
                self.assertEqual(observed['scope'], bundle['record_config']['scope'])
                self.assertEqual(observed['reported_post_count'], 1)
                self.assertFalse(self.verify(bundle)['site_outcome_verified'])


if __name__ == '__main__':
    unittest.main()
