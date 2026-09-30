"""Read-only inspection of one registered, published synthetic S1 adapter."""

import argparse
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Literal

from pydantic import Field

from .contracts import TypedModel, canonical, digest
from .dataset_audit import audit_snapshot
from .dataset_preflight import bounded_file
from .registries import AdapterRegistry


class AdapterRegistryInspectionReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['read_only_synthetic_adapter_registry_inspection'] = 'read_only_synthetic_adapter_registry_inspection'
    snapshot_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    adapter_id: str = Field(pattern='^decider-adapter-[a-f0-9]{64}$')
    deployment_id: str = Field(pattern='^decider-adapter-experiment-[a-f0-9]{64}$')
    artifact_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    candidate_report_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    status: Literal['available_experimental']
    runtime_enabled: Literal[False]
    promotion_authorized: Literal[False]


def inspect_registered_candidate(database: Path, dataset: Path, manifest: Path,
                                 report_path: Path) -> AdapterRegistryInspectionReport:
    manifest_bytes = bounded_file(manifest)
    pins = json.loads(manifest_bytes)
    identity = {'kind': 'decider_native_worker', 'real_model': True,
                'deployment_id': 'decider-' + digest(pins), 'pins': pins}
    with audit_snapshot(database) as (snapshot, database_identity):
        inspection = AdapterRegistry(SimpleNamespace(connection=snapshot)).inspect_synthetic_experiment(
            identity, dataset, manifest, report_path)
        result = AdapterRegistryInspectionReport(snapshot_sha256=database_identity['sha256'], **inspection)
    if bounded_file(manifest) != manifest_bytes:
        raise ValueError('adapter_registry_inspection_manifest_changed')
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description='Read-only synthetic S1 adapter registry inspection',
                                     allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    arguments = parser.parse_args()
    try:
        report = inspect_registered_candidate(arguments.database, arguments.dataset,
                                              arguments.manifest, arguments.report)
        print(canonical(report.model_dump()))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        parser.exit(1, 'Adapter registry inspection unavailable: private source or database identity changed. No deployment changed.\n')


if __name__ == '__main__':
    main()
