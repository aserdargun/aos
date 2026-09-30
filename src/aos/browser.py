import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from .contracts import AOSFault, ErrorCode, REPO_ROOT, TypedModel, canonical, digest, identifier


BROWSER_SCOPE = "aos://synthetic/form"
BROWSER_VALUE = "Hello from the local agent."
BROWSER_EXPECTED = {"value": BROWSER_VALUE, "receipt": BROWSER_VALUE, "submissions": 1}


class BrowserOutcome(TypedModel):
    value: str = Field(max_length=100)
    receipt: str = Field(max_length=100)
    submissions: int = Field(ge=0, le=100)


class DomElement(TypedModel):
    element_id: str = Field(pattern="^[a-f0-9]{32}$")
    role: Literal["textbox", "button"]
    label: Literal["Message", "Save locally"]


class DomObservation(BrowserOutcome):
    snapshot_id: str = Field(pattern="^[a-f0-9]{32}$")
    elements: list[DomElement] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_elements(self):
        if ([(element.role, element.label) for element in self.elements] !=
                [("textbox", "Message"), ("button", "Save locally")]
                or len({self.snapshot_id, *(element.element_id for element in self.elements)}) != 3):
            raise ValueError("Invalid symbolic form elements")
        return self


class BrowserRuntime:
    fixture_file = "browser_form.html"
    worker_file = "browser_worker.py"
    scope = BROWSER_SCOPE
    vision = False

    def __init__(self, manifest: Path):
        self.manifest = manifest
        self.runtime_id = identifier("browser")
        self.process = None
        self.buffer = b""
        self.evidence = {}
        self.pins = {}

    def command(self, root: Path) -> list[str]:
        packages = Path(importlib.metadata.distribution("playwright").locate_file(""))
        return ["/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session",
                "--cap-drop", "ALL", "--clearenv", "--ro-bind", "/usr", "/usr",
                "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib", "/lib64",
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run",
                "--dir", "/tmp/home", "--setenv", "HOME", "/tmp/home",
                "--setenv", "PATH", "/usr/bin", "--setenv", "PYTHONPATH", "/python",
                "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                "--ro-bind", str(packages), "/python", "--ro-bind", str(root), "/browser",
                "--ro-bind", str(REPO_ROOT / "src/aos" / self.worker_file), "/worker.py",
                "--ro-bind", str(REPO_ROOT / "examples" / self.fixture_file), "/fixture.html",
                "--chdir", "/tmp", "--", str(Path(sys.executable).resolve()), "-u", "/worker.py"]

    def start(self) -> None:
        if self.process is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, "Browser runtime already started")
        self.pins = json.loads(self.manifest.read_text())
        if not isinstance(self.pins, dict) or set(self.pins) != {
                "playwright_version", "browser_version", "revision", "browser_root", "files"}:
            raise AOSFault(ErrorCode.RUNTIME_CRASH, "Invalid browser manifest")
        try:
            installed_version = importlib.metadata.version("playwright")
        except importlib.metadata.PackageNotFoundError:
            raise AOSFault(ErrorCode.RUNTIME_CRASH, "Install the pinned browser extra first") from None
        if (self.pins["playwright_version"] != "1.63.0"
                or installed_version != self.pins["playwright_version"]
                or self.pins["revision"] != "1243" or self.pins["browser_version"] != "153.0.8010.12"):
            raise AOSFault(ErrorCode.RUNTIME_CRASH, "Browser version pin differs")
        root = Path(self.pins["browser_root"]).resolve(strict=True)
        files = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(root.rglob("*")) if path.is_file() and not path.is_symlink()}
        if not files or files != self.pins["files"] or any(path.is_symlink() for path in root.rglob("*")):
            raise AOSFault(ErrorCode.RUNTIME_CRASH, "Browser artifact hash differs")
        try:
            self.process = subprocess.Popen(self.command(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, env={}, start_new_session=True)
            self.evidence = self.receive(25)
            if (not self.evidence.get("ready") or self.evidence.get("browser_version") != self.pins["browser_version"]
                    or self.evidence.get("network_namespace") == os.readlink("/proc/self/ns/net")
                    or self.evidence.get("home_visible") is not False
                    or self.evidence.get("docker_socket_visible") is not False):
                raise AOSFault(ErrorCode.RUNTIME_CRASH, "Browser isolation handshake failed")
        except BaseException:
            self.stop()
            raise

    def receive(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], max(0, remaining))[0]:
                self.stop()
                raise AOSFault(ErrorCode.TIMEOUT, "Browser worker timed out; no host fallback")
            chunk = os.read(self.process.stdout.fileno(), 65537)
            if not chunk:
                self.stop()
                raise AOSFault(ErrorCode.RUNTIME_CRASH, "Isolated browser worker exited")
            self.buffer += chunk
            if len(self.buffer) > 65536:
                self.stop()
                raise AOSFault(ErrorCode.INVALID_OUTPUT, "Browser output exceeded limit")
        line, self.buffer = self.buffer.split(b"\n", 1)
        try:
            result = json.loads(line)
            if not isinstance(result, dict):
                raise ValueError("Expected object")
        except (ValueError, UnicodeError):
            self.stop()
            raise AOSFault(ErrorCode.INVALID_OUTPUT, "Malformed browser output") from None
        if "error" in result:
            try:
                code = ErrorCode(result["error"])
            except ValueError:
                code = ErrorCode.INVALID_OUTPUT
            raise AOSFault(code, "Isolated browser rejected the operation")
        return result

    def perform(self, tool: str, arguments: dict) -> dict:
        if self.process is None or self.process.poll() is not None:
            raise AOSFault(ErrorCode.RUNTIME_CRASH, "Browser runtime is not running")
        payload = (canonical({"tool": tool, "arguments": arguments}) + "\n").encode()
        if len(payload) > 4096:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, "Browser request exceeded limit")
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except OSError:
            self.stop()
            raise AOSFault(ErrorCode.RUNTIME_CRASH, "Browser worker disconnected") from None
        result = self.receive(10)
        try:
            return self.validate_result(tool, result)
        except (ValueError, ValidationError):
            self.stop()
            raise AOSFault(ErrorCode.INVALID_OUTPUT, "Invalid typed browser result") from None

    def validate_result(self, tool: str, result: dict) -> dict:
        if tool == "browser.observe":
            return DomObservation.model_validate(result).model_dump()
        if tool == "browser.verify":
            return BrowserOutcome.model_validate(result).model_dump()
        if result != {"applied": True}:
            raise ValueError("Invalid acknowledgement")
        return result

    def read(self, path: str) -> str:
        if path != BROWSER_SCOPE:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, "Only the synthetic form is authorized")
        return canonical(self.perform("browser.observe", {}))

    def status(self) -> dict:
        return {"kind": "bubblewrap_chromium", "runtime_id": self.runtime_id,
                "running": self.process is not None and self.process.poll() is None,
                "real_execution": True, "desktop": False, "network": False, "vision": self.vision,
                "scope": self.scope, "runtime_digest": digest(self.pins),
                "fixture_sha256": hashlib.sha256((REPO_ROOT / "examples" / self.fixture_file).read_bytes()).hexdigest(),
                "worker_sha256": hashlib.sha256((REPO_ROOT / "src/aos" / self.worker_file).read_bytes()).hexdigest(),
                "isolation": self.evidence}

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            process.stdin.close()
            process.stdout.close()
        self.buffer = b""
