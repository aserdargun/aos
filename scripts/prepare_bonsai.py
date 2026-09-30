import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
REVISION = "6ed5e12bf84b7a63069882c91dd9e9218647d17b"
BUILD = "9a9394a895b96003ca842a6041cb28ac49a108f7"
TAG = "prism-b10709-9a9394a"
ARCHIVE = f"llama-{TAG}-bin-linux-cuda-13.3-x64.tar.gz"
ARCHIVE_HASH = "7e01a434e513b373026c347cd008502ab04f6307d1cab71fcd4cea212b4fdbb0"
FILES = {
    "Ternary-Bonsai-2-27B-PQ2_0.gguf": "3907dc1658db1f78a9826bf8d5bcb8dc65db0d466388937af57f2294fae62ec1",
    "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf": "6807ede61d570bb86ba34b756a0fa109edc33668604de867c6ea6d8f1d631903",
}


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url, target, expected):
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(target.suffix + ".partial")
        print("Downloading", target.name, flush=True)
        with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as output:
            while chunk := response.read(4 * 1024 * 1024):
                output.write(chunk)
        if sha256(partial) != expected:
            raise ValueError("Downloaded artifact differs from the upstream SHA-256")
        partial.replace(target)
    if sha256(target) != expected:
        raise ValueError("Existing artifact hash mismatch")


def main():
    parser = argparse.ArgumentParser(description="Explicit Bonsai PQ2_0 + Q8 projector + pinned CUDA runtime preparation")
    parser.add_argument("--download", action="store_true", required=True)
    parser.parse_args()
    model_root = ROOT / "models" / ("bonsai-" + REVISION)
    runtime_root = ROOT / "models" / ("bonsai-runtime-" + TAG)
    archive = ROOT / "models" / ARCHIVE
    download(f"https://github.com/PrismML-Eng/llama.cpp/releases/download/{TAG}/{ARCHIVE}", archive, ARCHIVE_HASH)
    runtime_root.mkdir(exist_ok=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(runtime_root, filter="data")
    for name, expected in FILES.items():
        download(f"https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf/resolve/{REVISION}/{name}", model_root / name, expected)
    binaries = list(runtime_root.rglob("llama-server"))
    if len(binaries) != 1:
        raise ValueError("Expected exactly one pinned llama-server")
    native_libraries = set()
    for binary in (binaries[0], binaries[0].parent / "libggml-cuda.so"):
        linked = subprocess.run(["ldd", str(binary)], check=True, capture_output=True, text=True).stdout
        for line in linked.splitlines():
            path = line.split("=>", 1)[-1].strip().split(" ", 1)[0]
            if path.startswith("/"):
                library = Path(path).resolve(strict=True)
                if not library.is_relative_to(runtime_root):
                    native_libraries.add(library)
    manifest = {
        "source": "prism-ml/Ternary-Bonsai-2-27B-gguf", "checkpoint_revision": REVISION,
        "tokenizer_revision": REVISION, "projector_revision": REVISION,
        "code_revision": BUILD, "release_tag": TAG, "archive_sha256": ARCHIVE_HASH,
        "model_path": str(model_root), "model_files": FILES,
        "weights_file": "Ternary-Bonsai-2-27B-PQ2_0.gguf", "projector_file": "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf",
        "runtime_path": str(runtime_root), "server_path": str(binaries[0]),
        "runtime_files": {str(path.relative_to(runtime_root)): sha256(path)
                          for path in sorted(runtime_root.rglob("*")) if path.is_file()},
        "native_libraries": {str(path): sha256(path) for path in sorted(native_libraries)},
        "context_tokens": 16384, "parallel": 1, "gpu_layers": 99,
        "max_output_tokens": 512, "temperature": 0.0, "reasoning_budget": 0,
    }
    destination = ROOT / "models/bonsai-manifest.json"
    destination.write_text(json.dumps(manifest, indent=2) + "\n")
    print("Prepared experimental Bonsai manifest:", destination, flush=True)


if __name__ == "__main__":
    main()
