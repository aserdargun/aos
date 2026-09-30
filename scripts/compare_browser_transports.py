import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import statistics
import time

from aos.browser_operator import BrowserOperator
from aos.contracts import REPO_ROOT, Settings
from aos.decision import FixtureDecisionEngine
from aos.desktop import DesktopRuntime
from aos.desktop_browser import DesktopBrowserRuntime
from aos.desktop_mcp import DesktopMCPBrowserRuntime
from aos.reusable_decider import ReusableDeciderEngine
from aos.storage import TrajectoryStore


def summary(values):
    ordered = sorted(values)
    return {"count": len(ordered), "min_ms": ordered[0], "median_ms": statistics.median(ordered),
            "max_ms": ordered[-1]}


def model_phase(metrics):
    return {key: metrics[key] for key in ("load_ms", "inference_ms", "reused", "prepared_cpu",
                                         "input_tokens", "peak_vram_bytes") if key in metrics}


def verified_form(result, verifications, model_calls, engine_kind):
    expected_calls = 2 if engine_kind == "decider" else 0
    return (engine_kind in {"fixture", "decider"} and isinstance(result, dict)
            and result.get("status") == "succeeded" and result.get("verified") is True
            and result.get("real_model") is (engine_kind == "decider")
            and len(verifications) == 2
            and all(row == {"method": "independent_dom_equals", "result": "passed"}
                    for row in verifications)
            and len(model_calls) == expected_calls
            and all(row["role"] == "system1" and row["status"] == "ok"
                    and row["latency_ms"] is not None for row in model_calls))


def sample(root, transport, desktop_manifest, mcp_manifest, engine_kind="fixture",
           model_manifest=None, model_python=None, prewarm_decider=False):
    if engine_kind not in {"fixture", "decider"} or type(prewarm_decider) is not bool or (
            prewarm_decider and engine_kind != "decider"):
        raise ValueError("Unknown benchmark decision engine")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    settings = Settings(workspace=root / "workspace", database=root / "trajectory.db")
    desktop = DesktopRuntime(settings.workspace, desktop_manifest)
    runtime = None
    store = None
    timings = []
    result = None
    error = None
    actions = []
    verifications = []
    model_calls = []
    model_phases = []
    admission_ms = None
    desktop_ms = browser_ms = task_ms = None
    prewarm_ms = None
    task_started = None
    manifest_sha256 = None
    try:
        if engine_kind == "decider":
            manifest_sha256 = hashlib.sha256(model_manifest.read_bytes()).hexdigest()
        started = time.perf_counter()
        desktop.start()
        desktop_ms = (time.perf_counter() - started) * 1000
        runtime = (DesktopBrowserRuntime(desktop) if transport == "cdp"
                   else DesktopMCPBrowserRuntime(desktop, mcp_manifest))
        started = time.perf_counter()
        runtime.start()
        browser_ms = (time.perf_counter() - started) * 1000
        original_perform = runtime.perform

        def measured_perform(tool, arguments):
            invoked = time.perf_counter()
            try:
                return original_perform(tool, arguments)
            finally:
                timings.append({"tool": tool, "start_ms": (invoked - task_started) * 1000,
                                "elapsed_ms": (time.perf_counter() - invoked) * 1000})

        runtime.perform = measured_perform
        store = TrajectoryStore(settings.database)
        engine = (FixtureDecisionEngine() if engine_kind == "fixture" else
                  ReusableDeciderEngine(model_manifest, model_python, cpu_prewarm=prewarm_decider))
        if engine_kind == "decider":
            original_decide = engine.decide

            async def measured_decide(state, options):
                prediction = await original_decide(state, options)
                model_phases.append(model_phase(engine.last_metrics))
                return prediction

            engine.decide = measured_decide

        async def run_form():
            nonlocal admission_ms, prewarm_ms, task_ms, task_started
            try:
                if prewarm_decider:
                    prewarm_started = time.perf_counter()
                    engine.prewarm_idle()
                    await asyncio.shield(engine.preparation)
                    prewarm_ms = (time.perf_counter() - prewarm_started) * 1000
                task_started = time.perf_counter()
                try:
                    return await BrowserOperator(settings, store, runtime, engine).form()
                finally:
                    task_ms = (time.perf_counter() - task_started) * 1000
            finally:
                if engine_kind == "decider":
                    admission_ms = engine.preparation_metrics.get("pin_verify_ms")
                    await engine.close()

        result = asyncio.run(run_form())
        actions = [dict(row) for row in store.connection.execute("SELECT tool,status FROM actions ORDER BY rowid")]
        verifications = [dict(row) for row in store.connection.execute(
            "SELECT method,result FROM verifications ORDER BY rowid")]
        model_calls = [dict(row) for row in store.connection.execute(
            "SELECT role,status,latency_ms,input_tokens,peak_vram_bytes FROM model_calls ORDER BY rowid")]
        if (not verified_form(result, verifications, model_calls, engine_kind)
                or engine_kind == "decider" and hashlib.sha256(model_manifest.read_bytes()).hexdigest()
                != manifest_sha256):
            raise ValueError("Transport sample did not reach independently verified success")
    except Exception as caught:
        error = {"type": type(caught).__name__, "code": str(getattr(caught, "code", ""))}
    finally:
        if store is not None:
            if not actions:
                actions = [dict(row) for row in store.connection.execute("SELECT tool,status FROM actions ORDER BY rowid")]
            if not verifications:
                verifications = [dict(row) for row in store.connection.execute(
                    "SELECT method,result FROM verifications ORDER BY rowid")]
            if not model_calls:
                model_calls = [dict(row) for row in store.connection.execute(
                    "SELECT role,status,latency_ms,input_tokens,peak_vram_bytes FROM model_calls ORDER BY rowid")]
            store.close()
        if runtime is not None:
            runtime.stop()
        desktop.stop()
    first_effectful_tool = next((row["start_ms"] for row in timings
                                 if row["tool"] in {"browser.fill", "browser.submit"}), None)
    return {"transport": transport,
            "decision_engine": "deterministic_fixture" if engine_kind == "fixture" else "pinned_decider_reusable",
            "model_manifest_sha256": manifest_sha256, "result": result,
            "error": error, "desktop_start_ms": desktop_ms, "browser_start_ms": browser_ms,
            "cpu_prewarmed": prewarm_decider, "cpu_prewarm_ms": prewarm_ms,
            "admission_pin_verify_ms": admission_ms, "model_phases": model_phases,
            "task_ms": task_ms, "first_effectful_tool_ms": first_effectful_tool, "tool_calls": timings,
            "model_calls": model_calls, "actions": actions, "verifications": verifications}


def main():
    parser = argparse.ArgumentParser(description="Isolated visible-CDP versus visible-MCP form smoke")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--engine", choices=("fixture", "decider"), default="fixture")
    parser.add_argument("--prewarm-decider", action="store_true")
    parser.add_argument("--desktop-manifest", type=Path, default=REPO_ROOT / "models/desktop-manifest.json")
    parser.add_argument("--mcp-manifest", type=Path, default=REPO_ROOT / "models/desktop-mcp-v001/manifest.json")
    parser.add_argument("--model-manifest", type=Path, default=REPO_ROOT / "models/decider-manifest.json")
    parser.add_argument("--model-python", type=Path, default=Path.home() / ".venv/bin/python")
    arguments = parser.parse_args()
    if not 1 <= arguments.repeats <= 5:
        parser.error("Repeats must be between 1 and 5")
    if arguments.engine == "decider" and (not arguments.model_manifest.is_file()
                                          or not arguments.model_python.is_file()):
        parser.error("Pinned Decider manifest and Python runtime must exist")
    if arguments.prewarm_decider and arguments.engine != "decider":
        parser.error("CPU prewarm requires --engine decider")
    output = arguments.output.resolve()
    if not output.is_relative_to((REPO_ROOT / "data").resolve()):
        parser.error("Output must be inside the ignored data directory")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    logging.disable(logging.INFO)
    results = []
    for repeat in range(arguments.repeats):
        order = ("cdp", "mcp") if repeat % 2 == 0 else ("mcp", "cdp")
        for transport in order:
            measured = sample(output / f"{repeat + 1}-{transport}", transport,
                              arguments.desktop_manifest, arguments.mcp_manifest, arguments.engine,
                              arguments.model_manifest, arguments.model_python,
                              arguments.prewarm_decider)
            results.append(measured)
            (output / "report.json").write_text(json.dumps({"samples": results}, indent=2) + "\n")
            print(json.dumps({"repeat": repeat + 1, "transport": transport,
                              "status": (measured["result"] or {}).get("status"),
                              "desktop_start_ms": measured["desktop_start_ms"],
                              "browser_start_ms": measured["browser_start_ms"],
                              "cpu_prewarm_ms": measured["cpu_prewarm_ms"],
                              "admission_pin_verify_ms": measured["admission_pin_verify_ms"],
                              "model_phases": measured["model_phases"],
                              "task_ms": measured["task_ms"],
                              "first_effectful_tool_ms": measured["first_effectful_tool_ms"],
                              "error": measured["error"]}), flush=True)
    if any(row["error"] for row in results):
        raise SystemExit(1)
    for transport in ("cdp", "mcp"):
        group = [row for row in results if row["transport"] == transport]
        print(json.dumps({"transport": transport,
                          "desktop_start": summary([row["desktop_start_ms"] for row in group]),
                          "browser_start": summary([row["browser_start_ms"] for row in group]),
                          "first_effectful_tool": summary([row["first_effectful_tool_ms"] for row in group]),
                          "verified_task": summary([row["task_ms"] for row in group])}), flush=True)


if __name__ == "__main__":
    main()
