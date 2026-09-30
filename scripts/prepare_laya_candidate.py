import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys


REVISION = "f9ab0b228f0fc0f14d873dbc99038f135c2da1b2"
WHEEL_SHA256 = "e3b3aa7bb65c1a154db1b2718e3a7f5ecb1a506800933b519d640f438c5d8ad5"
MODEL_FILES = {
    "README.md": "59e2d2cf0203fb309d31ca6f51c4c6ab758fa32cecd82cfcd3ce20425edea7b6",
    "encoder/config.json": "5268d24ad3b77c8151de5dcb0762ba4391619aad9ab0bda33e36fb083cfeae6d",
    "model.safetensors": "4fa56de72383a9d3efa9cfa78955733c81b9fc8067a587ca4beb82c78107a24e",
    "rl_agent_config.json": "ebf0cd524d92342a6be5e48e9fca3d7c2babfb5a56ccd79d2171ef5d8c7f7be8",
    "tokenizer/tokenizer.json": "6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30",
    "tokenizer/tokenizer_config.json": "08d4cf3ac4dca381759441b85b91a6d40e688471dcd33d15d6649eb0a9a854d1",
}
DEPENDENCIES = ("torch", "transformers", "safetensors", "huggingface-hub", "numpy", "laya")


def hashes(root: Path, *, ignored=()):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in ignored for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError("Candidate files must not be symlinks")
        if path.is_file():
            with path.open("rb") as stream:
                result[str(relative)] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description="Pin an already-downloaded local Laya experiment")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    model = arguments.model.resolve(strict=True)
    sdk = arguments.sdk.resolve(strict=True)
    model_hashes = hashes(model, ignored={".cache"})
    if model_hashes != MODEL_FILES:
        raise ValueError("Candidate model file set differs from the pinned revision")
    with arguments.wheel.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != WHEEL_SHA256:
            raise ValueError("Laya SDK wheel hash differs")
    sys.path.insert(0, str(sdk))
    pins = {"source": "convaiinnovations/laya-typed-decisions", "checkpoint_revision": REVISION,
            "tokenizer_revision": REVISION, "sdk_version": "0.3.6", "wheel_sha256": WHEEL_SHA256,
            "model_path": str(model), "model_files": model_hashes,
            "sdk_path": str(sdk), "sdk_files": hashes(sdk, ignored={"__pycache__"}),
            "dependencies": {name: importlib.metadata.version(name) for name in DEPENDENCIES}}
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(pins, sort_keys=True, indent=2) + "\n")
    print(arguments.output)


if __name__ == "__main__":
    main()
