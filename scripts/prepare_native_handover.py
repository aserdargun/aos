import argparse
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from aos.contracts import canonical
from aos.native_handover import prepare_native_handover


def main():
    parser = argparse.ArgumentParser(description='Prepare a read-only native default-session maintenance preview; never execute a transition')
    parser.add_argument('--expected-session', required=True)
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    try:
        preview = prepare_native_handover(arguments.expected_session, arguments.output)
    except (ValueError, OSError, TypeError):
        parser.exit(1, 'Native handover preview unavailable; no transition authorized or executed.\n')
    print(canonical({'output': str(arguments.output), 'execution_authorized': False,
                     'local_idle_observed': preview.local_idle_observed, 'blockers': preview.blockers}))


if __name__ == '__main__':
    main()
