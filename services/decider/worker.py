import contextlib
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import select
import sys
import time


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_files(root, expected):
    root = Path(root).resolve(strict=True)
    actual = {str(path.relative_to(root)) for path in root.rglob("*")
              if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}
    if actual != set(expected):
        raise ValueError("Pinned file set differs")
    for relative, checksum in expected.items():
        path = root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts or sha256(path) != checksum:
            raise ValueError("Artifact hash mismatch")
    return root


def verify_environment(pins):
    for key in ("checkpoint_revision", "code_revision", "tokenizer_revision"):
        if len(pins[key]) != 40 or any(character not in "0123456789abcdef" for character in pins[key]):
            raise ValueError("Immutable revision required")
    model_root = verify_files(pins["model_path"], pins["model_files"])
    code_root = verify_files(pins["code_path"], pins["code_files"])
    actual_dependencies = {distribution.metadata["Name"].lower().replace("_", "-"): distribution.version
                           for distribution in importlib.metadata.distributions()}
    if actual_dependencies != pins["dependencies"]:
        raise ValueError("Dependency environment differs from deployment")
    sys.path.insert(0, str(code_root.parent))
    return model_root


def validate_request(request):
    if (not isinstance(request, dict) or set(request) != {"state", "question", "options"}
            or not isinstance(request["state"], str) or not isinstance(request["question"], str)
            or not isinstance(request["options"], list)):
        raise ValueError("Invalid decision request")
    options = request["options"]
    if any(not isinstance(option, dict) or set(option) != {"id", "label"}
           or not isinstance(option["id"], str) or not isinstance(option["label"], str) for option in options):
        raise ValueError("Invalid option structure")
    if not 2 <= len(options) <= 10 or len({option["id"] for option in options}) != len(options):
        raise ValueError("Invalid options")
    labels = [option["label"] for option in options]
    if len(set(labels)) != len(labels):
        raise ValueError("Duplicate option labels")
    return labels


class ModelSession:
    def __init__(self, model_root, pins):
        self.model_root = model_root
        self.pins = pins
        self.manifest = None
        self.model = None
        self.device = None
        self.inferences = 0
        self.idle_prepared = False
        self.gpu_idle_prepared = False
        self.deployment_digest = hashlib.sha256(json.dumps(pins, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    def prepare_cpu(self, *, idle=False):
        if self.model is not None:
            raise ValueError('Model is already prepared')
        started = time.perf_counter()
        with contextlib.redirect_stdout(sys.stderr):
            import torch
            from decider.infer import Decider

            if torch.cuda.is_initialized():
                raise ValueError('CPU preparation cannot own a CUDA context')
            self.model = Decider(str(self.model_root), device='cpu', use_graphs=False)
            self.device = 'cpu'
            if torch.cuda.is_initialized():
                raise ValueError('CPU preparation initialized CUDA')
        self.idle_prepared = idle
        return {'deployment_digest': self.deployment_digest, 'prepared_cpu': True,
                'cuda_initialized': False, 'idle_prepared': idle,
                'load_ms': (time.perf_counter() - started) * 1000}

    def admit_job_cpu(self):
        import torch

        if (not self.idle_prepared or self.device != 'cpu' or self.inferences
                or torch.cuda.is_initialized() or self.manifest is None):
            raise ValueError('Job admission requires an unused idle CPU model')
        started = time.perf_counter()
        if json.loads(self.manifest.read_text()) != self.pins:
            raise ValueError('Deployment manifest changed before job admission')
        if verify_environment(self.pins) != self.model_root:
            raise ValueError('Model root changed before job admission')
        if torch.cuda.is_initialized():
            raise ValueError('Job admission initialized CUDA')
        self.idle_prepared = False
        return {'deployment_digest': self.deployment_digest, 'job_admitted': True,
                'cuda_initialized': False, 'pin_verify_ms': (time.perf_counter() - started) * 1000}

    def prepare_idle_gpu(self):
        import torch

        if (self.model is None or self.device != 'cuda' or self.inferences < 1
                or self.idle_prepared or self.gpu_idle_prepared or not torch.cuda.is_initialized()):
            raise ValueError('GPU idle preparation requires a completed active inference')
        self.gpu_idle_prepared = True
        return {'deployment_digest': self.deployment_digest, 'idle_prepared': True,
                'cuda_initialized': True, 'job_released': True}

    def admit_job_gpu(self):
        import torch

        if (not self.gpu_idle_prepared or self.device != 'cuda' or self.model is None
                or not torch.cuda.is_initialized() or self.manifest is None):
            raise ValueError('GPU job admission requires an idle resident model')
        started = time.perf_counter()
        if json.loads(self.manifest.read_text()) != self.pins:
            raise ValueError('Deployment manifest changed before GPU job admission')
        if verify_environment(self.pins) != self.model_root:
            raise ValueError('Model root changed before GPU job admission')
        self.gpu_idle_prepared = False
        return {'deployment_digest': self.deployment_digest, 'job_admitted': True,
                'cuda_initialized': True, 'gpu_resident': True,
                'pin_verify_ms': (time.perf_counter() - started) * 1000}

    def infer(self, request):
        labels = validate_request(request)
        if self.idle_prepared or self.gpu_idle_prepared:
            raise ValueError('Idle model cannot authorize inference without fresh job admission')
        with contextlib.redirect_stdout(sys.stderr):
            import torch
            from decider.infer import Decider, Example, Q
            from decider.prompt import build

            if not torch.cuda.is_available():
                raise ValueError("CUDA required for real-model acceptance")
            torch.cuda.reset_peak_memory_stats()
            started = time.perf_counter()
            reused = self.inferences > 0
            load_ms = 0.0
            prepared_cpu = self.device == 'cpu'
            if self.model is None:
                self.model = Decider(str(self.model_root), device="cuda", use_graphs=False)
                self.device = 'cuda'
                torch.cuda.synchronize()
                load_ms = (time.perf_counter() - started) * 1000
            elif prepared_cpu:
                self.model.m.to('cuda')
                self.model.dev = 'cuda'
                self.device = 'cuda'
                torch.cuda.synchronize()
                load_ms = (time.perf_counter() - started) * 1000
            inference_started = time.perf_counter()

            class KeepOrder:
                def shuffle(self, values):
                    return None

            tokenizer = self.model.m.tok
            context_tokens = len(tokenizer.encode("Context:\n" + request["state"], add_special_tokens=False))
            item = build(Example(request["state"], [Q(request["question"], labels)]), tokenizer,
                         KeepOrder(), max_options=10, max_ctx_tokens=1536)
            if context_tokens > 1536 or len(item["ids"]) > 1536:
                raise ValueError("Token budget exceeded; truncation is forbidden")
            output = self.model.decide(request["state"], [{"question": request["question"], "options": labels}], max_ctx_tokens=1536)[0]
            torch.cuda.synchronize()
            finished = time.perf_counter()
            metrics = {"latency_ms": (finished - started) * 1000,
                       "load_ms": load_ms, "inference_ms": (finished - inference_started) * 1000, "reused": reused,
                       "prepared_cpu": prepared_cpu,
                       "input_tokens": len(item["ids"]), "peak_vram_bytes": torch.cuda.max_memory_allocated()}
        label_ids = {option["label"]: option["id"] for option in request["options"]}
        prediction = {"selected_option": label_ids[output["choice"]],
                      "probabilities": {label_ids[label]: probability for label, probability in output["probs"].items()}}
        self.inferences += 1
        return {"deployment_digest": self.deployment_digest, "prediction": prediction, "metrics": metrics}


class RequestLines:
    def __init__(self, descriptor, idle_seconds=75):
        if type(idle_seconds) not in (int, float) or not 0 < idle_seconds <= 630:
            raise ValueError("Idle deadline exceeds worker bound")
        self.descriptor = descriptor
        self.idle_seconds = idle_seconds
        self.buffer = b""

    def next(self):
        deadline = time.monotonic() + self.idle_seconds
        while b"\n" not in self.buffer:
            if len(self.buffer) >= 65536:
                raise ValueError("Request line exceeds bound")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.descriptor], [], [], remaining)[0]:
                raise TimeoutError("Decision worker idle deadline exceeded")
            chunk = os.read(self.descriptor, 65537 - len(self.buffer))
            if not chunk:
                if self.buffer:
                    raise ValueError("Incomplete request line")
                return None
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        if len(line) + 1 > 65536:
            raise ValueError("Request line exceeds bound")
        return line


def emit(response):
    encoded = json.dumps(response, allow_nan=False)
    if len(encoded.encode()) + 1 > 65536:
        raise ValueError("Decision response exceeds bound")
    print(encoded, flush=True)


def serve(session, descriptor, idle_seconds=75):
    lines = RequestLines(descriptor, idle_seconds=idle_seconds)
    request_ids = set()
    for request_number in range(256):
        try:
            line = lines.next()
        except TimeoutError:
            return
        if line is None:
            return
        envelope = json.loads(line)
        if (not isinstance(envelope, dict) or set(envelope) not in ({"request_id", "request"}, {"request_id", "operation"})
                or not isinstance(envelope["request_id"], str)
                or not re.fullmatch("[a-f0-9]{32}", envelope["request_id"])
                or envelope["request_id"] in request_ids):
            raise ValueError("Invalid decision request envelope")
        request_ids.add(envelope["request_id"])
        if 'operation' in envelope:
            if envelope['operation'] == 'prepare_cpu':
                response = session.prepare_cpu()
            elif envelope['operation'] == 'prepare_idle_cpu':
                response = session.prepare_cpu(idle=True)
            elif envelope['operation'] == 'admit_job_cpu':
                response = session.admit_job_cpu()
            elif envelope['operation'] == 'prepare_idle_gpu':
                response = session.prepare_idle_gpu()
            elif envelope['operation'] == 'admit_job_gpu':
                response = session.admit_job_gpu()
            else:
                raise ValueError('Unknown worker operation')
        else:
            response = session.infer(envelope['request'])
        emit({**response, "request_id": envelope["request_id"]})


def main():
    if len(sys.argv) not in {2, 3, 4} or len(sys.argv) >= 3 and sys.argv[-1] != "--serve":
        raise ValueError("Usage: worker.py MANIFEST [--idle-seconds=1..630] --serve")
    idle_seconds = 75
    if len(sys.argv) == 4:
        match = re.fullmatch(r"--idle-seconds=([1-9][0-9]*)", sys.argv[2])
        if match is None or int(match.group(1)) > 630:
            raise ValueError("Invalid Decider worker idle deadline")
        idle_seconds = int(match.group(1))
    pins = json.loads(Path(sys.argv[1]).read_text())
    session = ModelSession(verify_environment(pins), pins)
    session.manifest = Path(sys.argv[1])
    if len(sys.argv) >= 3:
        serve(session, sys.stdin.fileno(), idle_seconds=idle_seconds)
    else:
        encoded = sys.stdin.buffer.read(65537)
        if len(encoded) > 65536:
            raise ValueError("Decision request exceeds bound")
        emit(session.infer(json.loads(encoded)))


if __name__ == "__main__":
    main()
