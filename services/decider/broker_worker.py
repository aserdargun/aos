from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from broker_runtime import TurnGate, clock, encoded, object_json
from worker import ModelSession, validate_request, verify_environment


def main():
    if len(sys.argv) != 3:
        raise ValueError('Usage: broker_worker.py MANIFEST READY_FILE')
    manifest, ready = Path(sys.argv[1]), Path(sys.argv[2])
    gate = TurnGate(ready, 'aos.decider.turn.v1')
    pins = gate.manifest(manifest)
    raw = sys.stdin.buffer.read(128 * 1024 + 1)
    if not raw or len(raw) > 128 * 1024:
        raise ValueError('Broker Decider input exceeds its bound')
    payload = object_json(raw)
    if set(payload) != {'request'}:
        raise ValueError('Broker Decider payload differs')
    validate_request(payload['request'])
    session = ModelSession(verify_environment(pins), pins)
    if session.deployment_digest != gate.value['deployment_digest']:
        raise ValueError('Broker Decider deployment digest differs')
    session.manifest = manifest
    readiness = session.prepare_gpu()
    gate.check_manifest(manifest)
    deadline = gate.admit_inference()
    gate.check_manifest(manifest)
    response = session.infer(payload['request'])
    if clock() >= deadline:
        raise TimeoutError('Broker Decider inference expired')
    if response.get('deployment_digest') != gate.value['deployment_digest']:
        raise ValueError('Broker Decider response belongs to another deployment')
    response['metrics']['broker_activation_load_ms'] = readiness['load_ms']
    sys.stdout.buffer.write(encoded(response))
    sys.stdout.buffer.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
