import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset import validator
from aos.owned_skill_knowledge import validate_planning_knowledge_preview
from aos.owned_skill_knowledge_transport import (
    OwnedSkillKnowledgeCanonicalStartRequest, OwnedSkillKnowledgePreviewTransport,
    OwnedSkillKnowledgeReportTransport, preview_transport, report_transport)
from test_owned_skill_knowledge_service import FixtureKnowledgePlanner, make_fixture


class OwnedSkillKnowledgeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.planner = FixtureKnowledgePlanner()
        self.scheduler, self.store, self.service, self.arguments = make_fixture(self.root, self.planner)
        self.planning = self.scheduler.owned_skill_planning

    async def asyncTearDown(self):
        await self.planning.close()
        self.temporary.cleanup()

    def request(self, transport):
        preview = transport['preview']
        return {'schema_version': '1.1', 'preview_canonical': transport['preview_canonical'],
                'confirm_sha256': preview['confirm_sha256'], 'inference_consent': True, 'storage_consent': True,
                'lease_id': preview['authority']['lease_id'], 'generation': preview['authority']['generation']}

    def raw_hash(self, text):
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    async def test_float_zero_negative_zero_and_scientific_pins_survive_canonical_wire(self):
        for temperature, representation in ((0.0, '0.0'), (-0.0, '-0.0'), (1e-7, '1e-07')):
            with self.subTest(temperature=representation):
                self.planner.pins['temperature'] = temperature
                self.planner.identity['deployment_id'] = 'fixture-' + digest(self.planner.pins)
                preview = self.service.preview(**self.arguments)
                transport = preview_transport(preview)
                self.assertIn('"temperature":' + representation, transport['model_pins_canonical'])
                self.assertEqual(self.raw_hash(transport['preview_body_canonical']), preview['confirm_sha256'])
                self.assertEqual(self.raw_hash(transport['model_pins_canonical']), digest(preview['model_pins']))
                request = OwnedSkillKnowledgeCanonicalStartRequest.model_validate(self.request(transport))
                normalized = request.normalized_start()
                self.assertEqual(canonical(normalized['preview']), transport['preview_canonical'])
                self.assertIs(type(normalized['preview']['model_pins']['temperature']), float)
                self.service.begin(**normalized)
                await self.planning.task
                self.assertEqual(self.planning.status()['status'], 'ready')
                report = self.service.report(self.planning.status()['bundle_sha256'])
                wire = report_transport(report)
                self.assertEqual(wire['preview_canonical'], transport['preview_canonical'])
                self.assertEqual(wire['model_pins_canonical'], transport['model_pins_canonical'])
                knowledge = report['bundle']['knowledge']
                self.assertEqual(self.raw_hash(wire['intent_canonical']), knowledge['intent_sha256'])
                self.assertEqual(self.raw_hash(wire['model_request_canonical']), knowledge['dispatch']['request_sha256'])
                self.assertEqual(json.loads(wire['intent_canonical']), knowledge['intent'])
                self.assertEqual(json.loads(wire['model_request_canonical']), report['bundle']['model_request'])
                self.assertEqual(self.raw_hash(report['bundle_canonical']), report['planning_bundle_sha256'])

    def test_browser_integer_roundtrip_cannot_replace_pinned_preview_float(self):
        self.planner.pins['temperature'] = 0.0
        self.planner.identity['deployment_id'] = 'fixture-' + digest(self.planner.pins)
        preview = self.service.preview(**self.arguments)
        transport = preview_transport(preview)
        browser_value = json.loads(transport['preview_canonical'], parse_float=lambda value: int(float(value)))
        self.assertEqual(browser_value['model_pins']['temperature'], 0)
        self.assertIs(type(browser_value['model_pins']['temperature']), int)
        with self.assertRaises(ValueError):
            validate_planning_knowledge_preview(browser_value)
        request = OwnedSkillKnowledgeCanonicalStartRequest.model_validate(self.request(transport)).normalized_start()
        self.assertEqual(canonical(request['preview']), transport['preview_canonical'])

    def test_preview_and_start_transport_reject_noncanonical_or_mismatched_strings(self):
        transport = preview_transport(self.service.preview(**self.arguments))
        for field in ('preview_canonical', 'preview_body_canonical', 'model_pins_canonical'):
            for replacement in ('{}', transport[field] + ' '):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    OwnedSkillKnowledgePreviewTransport.model_validate(transport | {field: replacement})
        request = self.request(transport)
        for changes in ({'inference_consent': 1}, {'storage_consent': False}, {'extra': True},
                        {'preview_canonical': '{}'}, {'preview_canonical': transport['preview_canonical'] + ' '},
                        {'confirm_sha256': '0' * 64}, {'generation': True}, {'lease_id': 'wrong'}):
            with self.assertRaises(ValueError):
                OwnedSkillKnowledgeCanonicalStartRequest.model_validate(request | changes)
        duplicate = transport['preview_canonical'][:-1] + ',"schema_version":"1.0"}'
        with self.assertRaises(ValueError):
            OwnedSkillKnowledgeCanonicalStartRequest.model_validate(request | {'preview_canonical': duplicate})

    async def test_report_transport_rejects_float_text_drift_hash_and_json_join_mismatch(self):
        self.planner.pins['temperature'] = 0.0
        self.planner.identity['deployment_id'] = 'fixture-' + digest(self.planner.pins)
        preview = self.service.preview(**self.arguments)
        request = OwnedSkillKnowledgeCanonicalStartRequest.model_validate(self.request(preview_transport(preview)))
        self.service.begin(**request.normalized_start())
        await self.planning.task
        report = self.service.report(self.planning.status()['bundle_sha256'])
        wire = report_transport(report)
        for field in ('preview_canonical', 'preview_body_canonical', 'model_pins_canonical',
                      'intent_canonical', 'model_request_canonical'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                OwnedSkillKnowledgeReportTransport.model_validate(wire | {field: wire[field].replace('0.0', '0')})
        for changes in ({'planning_bundle_sha256': '0' * 64}, {'bundle_canonical': '{}'},
                        {'knowledge_applied': True}, {'model_request_verified': True}):
            with self.assertRaises(ValueError):
                OwnedSkillKnowledgeReportTransport.model_validate(wire | {'report': report | changes})
        forged = copy.deepcopy(report)
        forged['bundle']['model_request']['temperature'] = 1.0
        forged['bundle']['knowledge']['dispatch']['request_sha256'] = digest(forged['bundle']['model_request'])
        forged['bundle_canonical'] = canonical(forged['bundle'])
        forged['planning_bundle_sha256'] = digest(forged['bundle'])
        with self.assertRaises(ValueError):
            report_transport(forged)

    async def test_transport_schema_parity_and_validated_examples(self):
        preview = preview_transport(self.service.preview(**self.arguments))
        start = self.request(preview)
        self.service.begin(**OwnedSkillKnowledgeCanonicalStartRequest.model_validate(start).normalized_start())
        await self.planning.task
        report = report_transport(self.service.report(self.planning.status()['bundle_sha256']))
        contracts = {'preview_transport': (OwnedSkillKnowledgePreviewTransport, preview),
                     'report_transport': (OwnedSkillKnowledgeReportTransport, report),
                     'canonical_start_request': (OwnedSkillKnowledgeCanonicalStartRequest, start)}
        for name, (model, value) in contracts.items():
            model.model_validate(value)
            validator('owned_skill_knowledge_' + name).validate(value)
            schema = json.loads((REPO_ROOT / 'schemas' / ('owned_skill_knowledge_' + name + '.schema.json')).read_text())
            self.assertEqual({key: item for key, item in schema.items() if key != '$schema'}, model.model_json_schema())
