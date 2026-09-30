import argparse
import asyncio
import json
import logging
from pathlib import Path

from .computer import WorkspaceRuntime
from .browser import BrowserRuntime
from .browser_operator import BrowserOperator
from .contracts import AOSFault, REPO_ROOT, Settings
from .decision import DeciderEngine, FixtureDecisionEngine
from .desktop import DesktopRuntime
from .export import export_run
from .operator import Operator
from .storage import TrajectoryStore
from .supervisor import BonsaiSupervisor
from .vision import BonsaiVisionSupervisor, VisionRuntime
from .vision_operator import VisionOperator


def main() -> None:
    parser = argparse.ArgumentParser(description="AOS güvenli ilk dosya görevi")
    parser.add_argument("command", choices=["hello", "recover-hello", "browser-form", "vision-canvas", "export"])
    parser.add_argument("--workspace", type=Path, default=REPO_ROOT / "data/workspace")
    parser.add_argument("--database", type=Path, default=REPO_ROOT / "data/aos.sqlite")
    parser.add_argument("--engine", choices=["fixture", "decider"], default="decider")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--model-python", type=Path, default=REPO_ROOT / ".venv-decider/bin/python")
    parser.add_argument("--run-id")
    parser.add_argument("--bonsai-manifest", type=Path)
    parser.add_argument("--browser-manifest", type=Path, default=REPO_ROOT / "models/browser-manifest.json")
    parser.add_argument("--runtime", choices=["workspace", "desktop"], default="workspace")
    parser.add_argument("--desktop-manifest", type=Path, default=REPO_ROOT / "models/desktop-manifest.json")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if arguments.command != "export" and arguments.engine == "decider" and arguments.manifest is None:
        parser.error("Gerçek Decider için --manifest gerekli; test motoru için açıkça --engine fixture kullanın.")
    if arguments.command == "export" and not arguments.run_id:
        parser.error("Export için --run-id gerekli.")
    if arguments.command in {"recover-hello", "vision-canvas"} and arguments.bonsai_manifest is None:
        parser.error("Kurtarma veya vision kabulü için --bonsai-manifest gerekli.")
    if arguments.runtime == "desktop" and arguments.command not in {"hello", "recover-hello"}:
        parser.error("Desktop runtime yalnız hello/recover-hello komutlarında kullanılabilir.")
    settings = Settings(workspace=arguments.workspace, database=arguments.database)
    store = None
    runtime = BrowserRuntime(arguments.browser_manifest) if arguments.command == "browser-form" else WorkspaceRuntime(settings.workspace)
    if arguments.command == "vision-canvas":
        runtime = VisionRuntime(arguments.browser_manifest)
    elif arguments.runtime == "desktop":
        runtime = DesktopRuntime(settings.workspace, arguments.desktop_manifest)
    try:
        store = TrajectoryStore(settings.database, readonly=arguments.command == "export")
        if arguments.command == "export":
            print(json.dumps(export_run(store, arguments.run_id), ensure_ascii=False, indent=2))
            return
        runtime.start()
        engine = FixtureDecisionEngine() if arguments.engine == "fixture" else DeciderEngine(arguments.manifest, arguments.model_python)
        supervisor = BonsaiSupervisor(arguments.bonsai_manifest) if arguments.command == "recover-hello" else None
        if arguments.command == "vision-canvas":
            supervisor = BonsaiVisionSupervisor(arguments.bonsai_manifest)
            result = asyncio.run(VisionOperator(settings, store, runtime, engine, supervisor).canvas())
        elif arguments.command == "browser-form":
            result = asyncio.run(BrowserOperator(settings, store, runtime, engine).form())
        else:
            result = asyncio.run(Operator(settings, store, runtime, engine, supervisor).hello(recovery_probe=arguments.command == "recover-hello"))
        print(json.dumps(result, ensure_ascii=False))
        if not result.get("real_model"):
            logging.warning("Test motoru kullanıldı; gerçek Decider kabulü değildir.")
        if result["status"] != "succeeded":
            raise SystemExit(1)
    except (AOSFault, OSError, ValueError) as error:
        logging.error("Çalışma tamamlanamadı: %s", error.code if isinstance(error, AOSFault) else type(error).__name__)
        raise SystemExit(1) from None
    finally:
        runtime.stop()
        if store is not None:
            store.close()


if __name__ == "__main__":
    main()
