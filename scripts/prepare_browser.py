import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

from aos.contracts import REPO_ROOT


def main():
    parser = argparse.ArgumentParser(description="Prepare the explicitly synthetic, offline Chromium milestone")
    parser.add_argument("--download", action="store_true")
    arguments = parser.parse_args()
    if importlib.metadata.version("playwright") != "1.63.0":
        raise SystemExit("Install the pinned browser extra first: pip install -e '.[browser]'")
    cache = REPO_ROOT / "models/playwright"
    if arguments.download:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium", "--only-shell"],
                       env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(cache)}, check=True)
    root = cache / "chromium_headless_shell-1243/chrome-linux"
    if not (root / "chrome-headless-shell").is_file():
        root = cache / "chromium_headless_shell-1243/chrome-headless-shell-linux64"
    if not (root / "chrome-headless-shell").is_file():
        raise SystemExit("Pinned Chromium is absent; use --download")
    files = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(root.rglob("*")) if path.is_file()}
    manifest = {"playwright_version": "1.63.0", "browser_version": "153.0.8010.12",
                "revision": "1243", "browser_root": str(root), "files": files}
    output = REPO_ROOT / "models/browser-manifest.json"
    output.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Pinned {len(files)} Chromium files: {output}")


if __name__ == "__main__":
    main()
