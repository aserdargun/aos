import argparse
import json
from pathlib import Path
import subprocess

from .contracts import REPO_ROOT, canonical
from .dataset import convert_record, validate_record, validator


def converted_fixtures() -> list[dict]:
    examples = []
    for line in (REPO_ROOT / "examples/system1_choice.jsonl").read_text().splitlines():
        record = json.loads(line)
        validate_record("system1_choice", record)
        if record["provenance"]["synthetic"] is not True:
            raise ValueError("fixture_only_probe")
        examples.append(convert_record("system1_choice", record, "aos-tokenizer-probe"))
    return examples


def validate_probe_report(report: dict, examples: int, budget: int) -> None:
    if (not validator("dataset_tokenizer_probe").is_valid(report) or report["examples"] != examples
            or report["variants"] != examples * 40 or report["token_budget"] != budget
            or report["checkpoint_revision"] != report["tokenizer_revision"]
            or not report["max_context_tokens"] <= report["max_input_tokens"] <= report["max_padded_tokens"] <= budget):
        raise ValueError("tokenizer_report_invalid")


def tokenizer_preflight(manifest: Path, python: Path, source: Path, *, include_synthetic: bool = False, max_tokens: int = 1536) -> dict:
    if not include_synthetic:
        raise ValueError("synthetic_opt_in_required")
    if type(max_tokens) is not int or not 64 <= max_tokens <= 1536:
        raise ValueError("tokenizer_budget_invalid")
    examples = converted_fixtures()
    result = subprocess.run([str(python.absolute()), str(REPO_ROOT / "services/decider/tokenizer_probe.py"),
                             str(manifest.resolve(strict=True)), str(source.resolve(strict=True))],
                            input=canonical({"examples": examples, "max_tokens": max_tokens}), capture_output=True, text=True, timeout=120,
                            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                                 "TOKENIZERS_PARALLELISM": "false", "CUDA_VISIBLE_DEVICES": "", "OMP_NUM_THREADS": "1"})
    if result.returncode != 0 or len(result.stdout) > 65536:
        raise ValueError("tokenizer_preflight_failed")
    report = json.loads(result.stdout)
    validate_probe_report(report, len(examples), max_tokens)
    return report


def main():
    parser = argparse.ArgumentParser(description="Pinned yerel Decider tokenizer/batch provası; ağırlık/eğitim çalıştırmaz")
    parser.add_argument("--include-synthetic", action="store_true")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model-python", type=Path, required=True)
    parser.add_argument("--upstream-source", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, default=1536)
    arguments = parser.parse_args()
    try:
        print(canonical(tokenizer_preflight(arguments.manifest, arguments.model_python, arguments.upstream_source,
                                           include_synthetic=arguments.include_synthetic, max_tokens=arguments.max_tokens)))
    except (ValueError, OSError, subprocess.SubprocessError):
        parser.exit(1, "Tokenizer preflight başarısız; yerel pin, bağımlılık ve token bütçesini kontrol edin. Eğitim yapılmadı.\n")


if __name__ == "__main__":
    main()
