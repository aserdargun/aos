import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = "7789eb65d5cf519737608e218fa88819bddea0af"
CODE = "75b00fade2dd7f353106e3f4683e56fa2481ec28"
MODEL_FILES = ("config.json", "decider_config.json", "generation_config.json", "tokenizer.json",
               "tokenizer_config.json", "chat_template.jinja", "model.safetensors")
CODE_FILES = ("__init__.py", "infer.py", "model.py", "prompt.py")


def download(url, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        return
    partial = target.with_suffix(target.suffix + ".partial")
    print("Downloading", target.name, flush=True)
    with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as output:
        while chunk := response.read(4 * 1024 * 1024):
            output.write(chunk)
    partial.replace(target)


def hashes(directory):
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            with path.open("rb") as stream:
                result[str(path.relative_to(directory))] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description="Explicit local Decider artifact preparation; downloads about 4 GB")
    parser.add_argument("--download", action="store_true", required=True)
    parser.parse_args()
    model = ROOT / "models" / ("decider-" + CHECKPOINT)
    code = ROOT / "models" / ("decider-code-" + CODE) / "decider"
    for name in MODEL_FILES:
        download(f"https://huggingface.co/Mapika/decider-2b/resolve/{CHECKPOINT}/{name}", model / name)
    for name in CODE_FILES:
        download(f"https://raw.githubusercontent.com/Mapika/decider/{CODE}/decider/{name}", code / name)
    manifest = {"checkpoint_revision": CHECKPOINT, "tokenizer_revision": CHECKPOINT, "code_revision": CODE,
                "model_path": str(model), "code_path": str(code),
                "model_files": hashes(model), "code_files": hashes(code),
                "dependencies": {distribution.metadata["Name"].lower().replace("_", "-"): distribution.version
                                 for distribution in importlib.metadata.distributions()}}
    destination = ROOT / "models/decider-manifest.json"
    destination.write_text(json.dumps(manifest, indent=2) + "\n")
    print("Prepared experimental deployment:", destination, flush=True)


if __name__ == "__main__":
    main()
