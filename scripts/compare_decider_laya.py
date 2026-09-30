import argparse
import asyncio
import json
import logging
from pathlib import Path
import sqlite3
import statistics
import time

from aos.browser import BrowserRuntime
from aos.browser_operator import BrowserOperator
from aos.computer import WorkspaceRuntime
from aos.contracts import Option, REPO_ROOT, Settings, State
from aos.laya_candidate import LayaCandidateEngine
from aos.operator import Operator
from aos.reusable_decider import ReusableDeciderEngine
from aos.storage import TrajectoryStore
from aos.vision import FixtureVisionSupervisor, VisionRuntime
from aos.vision_operator import VisionOperator


def summarize(rows):
    values = [row["latency_ms"] for row in rows if row["role"] == "system1" and row["latency_ms"] is not None]
    return {"calls": len(values), "first_call_ms": values[0] if values else None,
            "later_call_ms": values[1:] if len(values) > 1 else [],
            "mean_call_ms": statistics.mean(values) if values else None,
            "max_peak_vram_bytes": max((row["peak_vram_bytes"] or 0 for row in rows), default=0)}


async def sample(root, kind, model, model_manifest, model_python, browser_manifest):
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    settings = Settings(workspace=root / "workspace", database=root / "trajectory.db")
    engine = (ReusableDeciderEngine(model_manifest, model_python, cpu_prewarm=False)
              if model == "decider" else LayaCandidateEngine(model_manifest, model_python))
    runtime = (WorkspaceRuntime(settings.workspace) if kind == "hello"
               else BrowserRuntime(browser_manifest) if kind == "browser_form"
               else VisionRuntime(browser_manifest))
    store = TrajectoryStore(settings.database)
    result = None
    error = None
    runtime_started = False
    started = time.perf_counter()
    try:
        runtime.start()
        runtime_started = True
        if kind == "hello":
            result = await Operator(settings, store, runtime, engine).hello()
        elif kind == "browser_form":
            result = await BrowserOperator(settings, store, runtime, engine).form()
        else:
            result = await VisionOperator(settings, store, runtime, engine, FixtureVisionSupervisor()).canvas()
    except Exception as caught:
        error = {"type": type(caught).__name__, "code": str(getattr(caught, "code", ""))}
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        await engine.close()
        if runtime_started:
            runtime.stop()
    rows = [dict(row) for row in store.connection.execute(
        "SELECT role,status,latency_ms,peak_vram_bytes FROM model_calls ORDER BY rowid")]
    decisions = [dict(row) for row in store.connection.execute(
        "SELECT selected_option,confidence,policy_result FROM decisions ORDER BY rowid")]
    actions = [dict(row) for row in store.connection.execute(
        "SELECT tool,status FROM actions ORDER BY rowid")]
    verifications = [dict(row) for row in store.connection.execute(
        "SELECT method,result FROM verifications ORDER BY rowid")]
    store.close()
    return {"model": model, "task": kind, "result": result, "error": error, "elapsed_ms": elapsed_ms,
            "model_calls": rows, "decisions": decisions, "actions": actions, "verifications": verifications,
            "summary": summarize(rows), "system2_fixture": kind == "vision_canvas"}


async def choice_probe(model, manifest, model_python, state, options):
    engine = (ReusableDeciderEngine(manifest, model_python, cpu_prewarm=False)
              if model == "decider" else LayaCandidateEngine(manifest, model_python))
    choices = []
    try:
        for _ in range(2):
            started = time.perf_counter()
            prediction = await engine.decide(state, options)
            choices.append({"selected_option": prediction.selected_option,
                            "confidence": prediction.probabilities[prediction.selected_option],
                            "elapsed_ms": (time.perf_counter() - started) * 1000,
                            "worker_metrics": engine.last_metrics})
    finally:
        await engine.close()
    return {"model": model, "task": "hello_choice_only_no_actions", "choices": choices}


async def main():
    parser = argparse.ArgumentParser(description="Private same-task AOS S1 candidate comparison")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-python", type=Path, default=Path("/home/cachyos/.venv/bin/python"))
    parser.add_argument("--decider-manifest", type=Path, default=REPO_ROOT / "models/decider-manifest.json")
    parser.add_argument("--laya-manifest", type=Path, default=REPO_ROOT / "models/laya-candidate-manifest.json")
    parser.add_argument("--browser-manifest", type=Path, default=REPO_ROOT / "models/browser-manifest.json")
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    if not output.is_relative_to((REPO_ROOT / "data").resolve()):
        parser.error("Comparison output must stay under the ignored data directory")
    arguments.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    logging.disable(logging.INFO)
    samples = []
    for kind in ("hello", "browser_form", "vision_canvas"):
        for model, manifest in (("decider", arguments.decider_manifest), ("laya_candidate", arguments.laya_manifest)):
            entry = await sample(arguments.output / f"{kind}-{model}", kind, model, manifest,
                                 arguments.model_python, arguments.browser_manifest)
            samples.append(entry)
            print(json.dumps({"model": model, "task": kind, "status": (entry["result"] or {}).get("status"),
                              "error": entry["error"], "elapsed_ms": round(entry["elapsed_ms"], 1),
                              "decisions": entry["decisions"]}), flush=True)
            (arguments.output / "report.json").write_text(json.dumps({"samples": samples}, indent=2) + "\n")
    connection = sqlite3.connect(arguments.output / "hello-decider" / "trajectory.db")
    state_json, options_json = connection.execute(
        "SELECT state_snapshots.state_json,decisions.options_json FROM decisions "
        "JOIN state_snapshots USING(snapshot_id) ORDER BY decisions.rowid LIMIT 1").fetchone()
    connection.close()
    state = State.model_validate_json(state_json)
    options = [Option.model_validate(option) for option in json.loads(options_json)]
    probes = []
    for model, manifest in (("decider", arguments.decider_manifest), ("laya_candidate", arguments.laya_manifest)):
        probe = await choice_probe(model, manifest, arguments.model_python, state, options)
        probes.append(probe)
        print(json.dumps({"choice_probe": probe}), flush=True)
    (arguments.output / "report.json").write_text(json.dumps({"samples": samples, "choice_probes": probes}, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
