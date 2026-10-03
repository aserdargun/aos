import argparse
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from aos.native_decider_entry import exec_native_decider_entry


def main():
    parser = argparse.ArgumentParser(description='Staged native-only Decider lifetime entry; fixed reviewed config required')
    parser.add_argument('--entry-config-sha256', required=True)
    parser.add_argument('worker_arguments', nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    worker_arguments = arguments.worker_arguments
    if worker_arguments and worker_arguments[0] == '--':
        worker_arguments = worker_arguments[1:]
    try:
        exec_native_decider_entry(arguments.entry_config_sha256, worker_arguments)
    except (ValueError, OSError, TypeError, RuntimeError):
        parser.exit(1, 'Staged native Decider entry denied; no model worker admitted.\n')


if __name__ == '__main__':
    main()
