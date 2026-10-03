import hashlib
import json
from pathlib import Path

from .contracts import AOSFault, ErrorCode, canonical, digest, now
from .dataset_adapter_candidate import private_report_bytes, verify_published_candidate
from .dataset_preflight import bounded_file
from .storage import TrajectoryStore


class ModelRegistry:
    def __init__(self, store: TrajectoryStore):
        self.store = store

    @staticmethod
    def experiment_values(identity: dict) -> dict:
        if identity.get('kind') == 'owned_episode_adapter_runtime':
            from .owned_adapter_identity import validate_adapter_identity

            validate_adapter_identity(identity)
        pins = identity["pins"]
        bonsai = identity["kind"] in {"bonsai_native_supervisor", "scientist_bonsai_broker"}
        laya = identity["kind"] == "laya_candidate"
        model_hash = pins["model_files"][pins["weights_file"] if bonsai else "model.safetensors"]
        model_id = ("bonsai-base-" if bonsai else "laya-candidate-" if laya else "decider-base-") + model_hash
        return {"model_id": model_id, "source": pins["source"] if bonsai or laya else "Mapika/decider-2b", "revision": pins["checkpoint_revision"],
                "sha256": model_hash, "backend": "prism_llama_cpp_cuda" if bonsai else "pytorch_cuda", "enabled": 0,
                "metadata_json": canonical({"tokenizer_revision": pins["tokenizer_revision"],
                                             "precision": "PQ2_0" if bonsai else "fp32_weights_bf16_autocast" if laya else "bf16", "capabilities": ["reasoning"] if bonsai else ["action_selection"],
                                             "acceptance": "experimental; explicit promotion required"})}

    def record_experiment(self, identity: dict) -> str:
        values = self.experiment_values(identity)
        model_id = values['model_id']
        existing = self.store.connection.execute("SELECT * FROM models WHERE model_id=?", (model_id,)).fetchone()
        if existing is None:
            self.store.insert("models", **values)
        elif any(existing[key] != value for key, value in values.items()):
            raise AOSFault(ErrorCode.MODEL_FAILURE, "Model registry identity differs")
        return model_id


class DeploymentRegistry:
    def __init__(self, store: TrajectoryStore):
        self.store = store

    def record_experiment(self, identity: dict) -> None:
        if not identity["real_model"]:
            return
        with self.store.connection:
            adapter_id = None
            if identity.get('kind') == 'owned_episode_adapter_runtime':
                from .owned_adapter_identity import adapter_registry_values

                adapter_values = adapter_registry_values(identity)
            model_id = ModelRegistry(self.store).record_experiment(identity)
            if identity.get('kind') == 'owned_episode_adapter_runtime':
                adapter_id = adapter_values['adapter_id']
                existing_adapter = self.store.connection.execute(
                    'SELECT * FROM adapters WHERE adapter_id=?', (adapter_id,)).fetchone()
                if existing_adapter is None:
                    self.store.insert('adapters', **adapter_values)
                elif any(existing_adapter[key] != value for key, value in adapter_values.items()):
                    raise AOSFault(ErrorCode.MODEL_FAILURE, 'Immutable owned adapter differs')
            deployment_id = identity["deployment_id"]
            configuration = canonical(identity["pins"])
            existing = self.store.connection.execute("SELECT * FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
            if existing is None:
                self.store.insert("deployments", deployment_id=deployment_id, model_id=model_id, adapter_id=adapter_id,
                                  status="EXPERIMENTAL", config_json=configuration,
                                  config_sha256=digest(identity["pins"]), created_at=now())
            elif existing["config_json"] != configuration or existing["model_id"] != model_id:
                raise AOSFault(ErrorCode.MODEL_FAILURE, "Immutable deployment configuration differs")
            if adapter_id is not None:
                from .owned_adapter_identity import verify_adapter_registry

                if not verify_adapter_registry(self.store.connection, identity):
                    raise AOSFault(ErrorCode.MODEL_FAILURE, 'Owned adapter registry binding changed')


class AdapterRegistry:
    def __init__(self, store: TrajectoryStore):
        self.store = store

    def record_synthetic_experiment(self, identity: dict, dataset: Path, manifest: Path,
                                    report_path: Path) -> tuple[str, str]:
        if identity.get('kind') != 'decider_native_worker' or identity.get('real_model') is not True:
            raise ValueError('adapter_experiment_requires_real_decider_identity')
        pins = identity['pins']
        manifest_bytes = bounded_file(manifest)
        if pins != json.loads(manifest_bytes) or identity['deployment_id'] != 'decider-' + digest(pins):
            raise ValueError('adapter_experiment_deployment_identity')
        report = verify_published_candidate(dataset, manifest, report_path)
        report_bytes = private_report_bytes(report_path.absolute())
        artifact_sha256 = report.train_report.candidate_artifact_sha256
        report_sha256 = hashlib.sha256(report_bytes).hexdigest()
        model_hash = pins['model_files']['model.safetensors']
        if model_hash != report.train_report.weights_sha256:
            raise ValueError('adapter_experiment_base_identity')
        adapter_id = 'decider-adapter-' + artifact_sha256
        deployment_id = 'decider-adapter-experiment-' + digest({
            'base_deployment_id': identity['deployment_id'], 'adapter_id': adapter_id,
            'report_sha256': report_sha256})
        metadata = canonical({'candidate_report_sha256': report_sha256,
                              'dataset_id': report.dataset_id,
                              'dataset_manifest_sha256': report.dataset_manifest_sha256,
                              'deployment_manifest_sha256': report.deployment_manifest_sha256,
                              'adapter_target': report.train_report.adapter_target,
                              'adapter_rank': report.train_report.adapter_rank,
                              'synthetic': True, 'training_ready': False,
                              'promotion_authorized': False})
        config = {'base_deployment_id': identity['deployment_id'],
                  'adapter_id': adapter_id, 'candidate_report_sha256': report_sha256,
                  'synthetic': True, 'runtime_enabled': False}
        configuration = canonical(config)
        with self.store.connection:
            if (bounded_file(manifest) != manifest_bytes
                    or private_report_bytes(report_path.absolute()) != report_bytes
                    or verify_published_candidate(dataset, manifest, report_path) != report):
                raise ValueError('adapter_experiment_sources_changed')
            model_id = ModelRegistry(self.store).record_experiment(identity)
            adapter_values = {'adapter_id': adapter_id, 'model_id': model_id, 'base_sha256': model_hash,
                              'sha256': artifact_sha256, 'compatibility': 'unknown',
                              'metadata_json': metadata}
            existing_adapter = self.store.connection.execute(
                'SELECT * FROM adapters WHERE adapter_id=?', (adapter_id,)).fetchone()
            if existing_adapter is None:
                self.store.insert('adapters', **adapter_values)
            elif any(existing_adapter[key] != value for key, value in adapter_values.items()):
                raise AOSFault(ErrorCode.MODEL_FAILURE, 'Immutable adapter registry identity differs')
            existing_deployment = self.store.connection.execute(
                'SELECT * FROM deployments WHERE deployment_id=?', (deployment_id,)).fetchone()
            if existing_deployment is None:
                self.store.insert('deployments', deployment_id=deployment_id, model_id=model_id,
                                  adapter_id=adapter_id, status='EXPERIMENTAL',
                                  config_json=configuration, config_sha256=digest(config), created_at=now())
            elif (existing_deployment['model_id'] != model_id
                  or existing_deployment['adapter_id'] != adapter_id
                  or existing_deployment['status'] != 'EXPERIMENTAL'
                  or existing_deployment['config_json'] != configuration
                  or existing_deployment['config_sha256'] != digest(config)):
                raise AOSFault(ErrorCode.MODEL_FAILURE, 'Immutable adapter deployment differs')
        return adapter_id, deployment_id

    def inspect_synthetic_experiment(self, identity: dict, dataset: Path, manifest: Path,
                                     report_path: Path) -> dict:
        if identity.get('kind') != 'decider_native_worker' or identity.get('real_model') is not True:
            raise ValueError('adapter_inspection_requires_real_decider_identity')
        pins = identity['pins']
        manifest_bytes = bounded_file(manifest)
        if pins != json.loads(manifest_bytes) or identity['deployment_id'] != 'decider-' + digest(pins):
            raise ValueError('adapter_inspection_deployment_identity')
        report = verify_published_candidate(dataset, manifest, report_path)
        report_bytes = private_report_bytes(report_path.absolute())
        artifact_sha256 = report.train_report.candidate_artifact_sha256
        report_sha256 = hashlib.sha256(report_bytes).hexdigest()
        model_hash = pins['model_files']['model.safetensors']
        adapter_id = 'decider-adapter-' + artifact_sha256
        deployment_id = 'decider-adapter-experiment-' + digest({
            'base_deployment_id': identity['deployment_id'], 'adapter_id': adapter_id,
            'report_sha256': report_sha256})
        model = self.store.connection.execute('SELECT * FROM models WHERE model_id=?',
                                              ('decider-base-' + model_hash,)).fetchone()
        adapter = self.store.connection.execute('SELECT * FROM adapters WHERE adapter_id=?',
                                                (adapter_id,)).fetchone()
        deployment = self.store.connection.execute('SELECT * FROM deployments WHERE deployment_id=?',
                                                   (deployment_id,)).fetchone()
        active = self.store.connection.execute('SELECT 1 FROM active_deployments WHERE deployment_id=?',
                                               (deployment_id,)).fetchone()
        expected_metadata = canonical({'candidate_report_sha256': report_sha256,
                                       'dataset_id': report.dataset_id,
                                       'dataset_manifest_sha256': report.dataset_manifest_sha256,
                                       'deployment_manifest_sha256': report.deployment_manifest_sha256,
                                       'adapter_target': report.train_report.adapter_target,
                                       'adapter_rank': report.train_report.adapter_rank,
                                       'synthetic': True, 'training_ready': False,
                                       'promotion_authorized': False})
        expected_config = {'base_deployment_id': identity['deployment_id'],
                           'adapter_id': adapter_id, 'candidate_report_sha256': report_sha256,
                           'synthetic': True, 'runtime_enabled': False}
        expected_model = ModelRegistry.experiment_values(identity)
        if (model is None or adapter is None or deployment is None or active is not None
                or any(model[key] != value for key, value in expected_model.items())
                or adapter['model_id'] != model['model_id'] or adapter['base_sha256'] != model_hash
                or adapter['sha256'] != artifact_sha256 or adapter['compatibility'] != 'unknown'
                or adapter['metadata_json'] != expected_metadata
                or deployment['model_id'] != model['model_id'] or deployment['adapter_id'] != adapter_id
                or deployment['status'] != 'EXPERIMENTAL'
                or deployment['config_json'] != canonical(expected_config)
                or deployment['config_sha256'] != digest(expected_config)
                or bounded_file(manifest) != manifest_bytes
                or private_report_bytes(report_path.absolute()) != report_bytes
                or verify_published_candidate(dataset, manifest, report_path) != report):
            raise ValueError('adapter_experiment_registry_or_source_changed')
        return {'adapter_id': adapter_id, 'deployment_id': deployment_id,
                'artifact_sha256': artifact_sha256, 'candidate_report_sha256': report_sha256,
                'status': 'available_experimental', 'runtime_enabled': False,
                'promotion_authorized': False}
