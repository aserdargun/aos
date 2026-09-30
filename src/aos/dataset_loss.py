import argparse
import json
import math
from pathlib import Path
import subprocess

from .contracts import REPO_ROOT, canonical
from .dataset import validator
from .dataset_tokenizer import converted_fixtures


def validate_loss_report(report: dict) -> None:
    if (not validator("dataset_loss_probe").is_valid(report)
            or [item["layout"] for item in report["layouts"]] != ["state_first", "schema_first"]
            or report["peak_vram_reserved_bytes"] < report["peak_vram_allocated_bytes"]
            or not math.isfinite(report["elapsed_seconds"]) or any(not math.isfinite(item["mean_nll"]) for item in report["layouts"])):
        raise ValueError("loss_report_invalid")


def loss_preflight(manifest: Path, python: Path, source: Path, *, include_synthetic: bool = False, forward_only: bool = False) -> dict:
    if not include_synthetic or not forward_only:
        raise ValueError("explicit_synthetic_forward_opt_in_required")
    result = subprocess.run([str(python.absolute()), str(REPO_ROOT / "services/decider/loss_probe.py"),
                             str(manifest.resolve(strict=True)), str(source.resolve(strict=True))],
                            input=canonical({"mode": "synthetic_forward_only", "examples": converted_fixtures()}), capture_output=True, text=True, timeout=180,
                            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                                 "TOKENIZERS_PARALLELISM": "false", "OMP_NUM_THREADS": "1"})
    if result.returncode != 0 or len(result.stdout) > 65536:
        raise ValueError("loss_preflight_failed")
    report = json.loads(result.stdout)
    validate_loss_report(report)
    return report


def main():
    parser = argparse.ArgumentParser(description="Sentetik gerçek Decider forward/loss; gradient/optimizer/eğitim yok")
    parser.add_argument("--include-synthetic", action="store_true")
    parser.add_argument("--forward-only", action="store_true")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model-python", type=Path, required=True)
    parser.add_argument("--upstream-source", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        print(canonical(loss_preflight(arguments.manifest, arguments.model_python, arguments.upstream_source,
                                      include_synthetic=arguments.include_synthetic, forward_only=arguments.forward_only)))
    except (ValueError, OSError, subprocess.SubprocessError):
        parser.exit(1, "Loss preflight başarısız; yerel pin/ortam ve GPU kaynaklarını kontrol edin. Eğitim yapılmadı.\n")


if __name__ == "__main__":
    main()
