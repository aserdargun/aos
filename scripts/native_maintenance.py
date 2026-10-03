import argparse
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from aos.contracts import canonical
from aos.native_handover import _json
from aos.native_maintenance import (NativeMaintenanceRequest, execute_native_maintenance,
                                   provision_native_maintenance_store)
from aos.shared_desktop_plan import read_pinned_file


def main():
    parser = argparse.ArgumentParser(description='Explicit default-native maintenance; never automatically restarts native')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('provision-store', help='Explicitly allocate a new fixed private maintenance store only')
    execute = commands.add_parser('execute', help='Interrupt exact reviewed default session and promote shared-only source')
    execute.add_argument('--request', type=Path, required=True)
    execute.add_argument('--request-file-sha256', required=True)
    execute.add_argument('--confirm-request-sha256', required=True)
    arguments = parser.parse_args()
    if arguments.command == 'provision-store':
        value = provision_native_maintenance_store()
    else:
        request = NativeMaintenanceRequest.model_validate(_json(read_pinned_file(
            arguments.request, arguments.request_file_sha256, private=True)))
        value = execute_native_maintenance(request, confirm_request_sha256=arguments.confirm_request_sha256)
    print(canonical(value.model_dump(mode='json')))


if __name__ == '__main__':
    main()
