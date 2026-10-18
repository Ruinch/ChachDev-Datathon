"""Create a local environment fingerprint without keys, videos, or annotation data."""
import argparse
import datetime
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from ml_tools.install_runtime import DEFAULT_LOCK, PROJECT_ROOT, _sha256, verify_installed

# A whitelist prevents accidental inclusion of access keys, file paths, prompts, or data.
SETTING_NAMES = (
    "APP_MAX_UPLOAD_MB", "APP_MAX_VIDEO_SECONDS", "APP_UPLOAD_TIMEOUT_SECONDS",
    "APP_ANALYSIS_TIMEOUT_SECONDS", "APP_FFPROBE_TIMEOUT_SECONDS", "APP_ML_BUDGET_SECONDS",
    "APP_ML_FRAMES", "APP_ML_INPUT_MODE", "APP_ML_MAX_SIDE", "APP_EXPERIMENTAL_REVIEW",
)


def build_report(root=PROJECT_ROOT, *, verify_model=False, lock=DEFAULT_LOCK):
    root = Path(root)
    fingerprint_files = (
        "constraints.txt", "pyproject.toml", "app/local_analyzer.py", "app/ml/model.py",
        "app/ml/rules.py", "app/ml/video.py", "app/ml/media_input.py",
        "ml_tools/start_model.ps1", "scripts/start-ml.ps1",
    )
    fingerprints = {name: _sha256(root / name) for name in fingerprint_files if (root / name).is_file()}
    if lock.is_file():
        fingerprints["selected_model_lock"] = _sha256(lock)
    catalog = os.environ.get("APP_RULE_CATALOG_PATH")
    if catalog and Path(catalog).is_file():
        fingerprints["selected_rule_catalog"] = _sha256(Path(catalog))
    report = {
        "schema_version": 1,
        "created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "python": platform.python_version(),
        "system": {"name": platform.system(), "release": platform.release(), "architecture": platform.machine()},
        "packages": dict(sorted((dist.metadata["Name"], dist.version) for dist in importlib.metadata.distributions())),
        "settings_overrides": {name: os.environ[name] for name in SETTING_NAMES if name in os.environ},
        "sha256": fingerprints,
        "model_files_verified": False,
        "inference_quality_tested": False,
        "gpu": None,
    }
    executable = shutil.which("nvidia-smi")
    if executable:
        try:
            result = subprocess.run(
                [executable, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5, check=True,
            )
            report["gpu"] = result.stdout.strip().splitlines()
        except (OSError, subprocess.SubprocessError):
            report["gpu"] = "unavailable"
    if verify_model:
        manifest = verify_installed(lock, root / ".local_ml")
        report["model_files_verified"] = True
        report["model"] = {name: manifest[name] for name in ("base_model", "model_revision", "llama_release") if name in manifest}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--verify-model", action="store_true")
    args = parser.parse_args()
    report = build_report(verify_model=args.verify_model, lock=args.lock)
    # Reports never silently replace a previous experiment fingerprint.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    print("Local environment report saved. It does not establish model quality or public availability.")


if __name__ == "__main__":
    main()
