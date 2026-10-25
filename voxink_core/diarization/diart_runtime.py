"""Owned runtime contract for the optional, isolated Diart worker.

The service and offline worker share inspection, environment, and audit helpers
here. Installation, downloads, checkpoint adaptation, and deletion remain in
``voxink_dev.diart_runtime`` and are never imported by the serving process.
The established experimental default is retained for existing local sessions.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path


DEFAULT_RUNTIME = Path(__file__).resolve().parents[2] / ".experiments/diart-20260907"
OWNER = "voxink-diart-experiment-v1"
MARKER = ".voxink-diart-owned.json"
DIRECTORIES = (".venv", "models", "cache", "tmp", "logs", "home", "outputs")


def validate_runtime_path(root: Path) -> Path:
    root = Path(os.path.abspath(root))
    if root.parent.name != ".experiments" or not root.name.startswith("diart-"):
        raise ValueError("Runtime must be a diart-* directory directly under .experiments")
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("Refusing a runtime with a symlink in its root path")
    return root


def validate_runtime(root: Path) -> dict:
    root = validate_runtime_path(root)
    if any((root / name).is_symlink() for name in DIRECTORIES):
        raise ValueError("Runtime-owned top-level directories must not be symlinks")
    for name in (MARKER, "manifest.json"):
        if (root / name).is_symlink():
            raise ValueError("Ownership metadata must not be a symlink")
    try:
        marker = json.loads((root / MARKER).read_text())
        manifest = json.loads((root / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("Runtime ownership metadata is missing or invalid") from exc
    if not marker.get("identity") or any(
        marker.get(key) != manifest.get(key) for key in ("owner", "runtime_root", "identity")
    ) or marker.get("owner") != OWNER or marker.get("runtime_root") != str(root):
        raise ValueError("Runtime ownership metadata does not match this exact path")
    return manifest


def record_action(root: Path, action: str, **details: object) -> None:
    with (root / "INSTALL-ACTIONS.jsonl").open("a") as output:
        output.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "action": action, **details}) + "\n")


def runtime_environment(root: Path, *, offline: bool = True) -> dict[str, str]:
    root = validate_runtime_path(root)
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith("PIP_"):
            environment.pop(key)
    for key in ("PYTHONHOME", "PYTHONPATH", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_TOKEN_PATH"):
        environment.pop(key, None)
    environment.update({
        "HOME": str(root / "home"),
        "VIRTUAL_ENV": str(root / ".venv"),
        "PATH": str(root / ".venv/bin") + os.pathsep + os.environ.get("PATH", ""),
        "TMPDIR": str(root / "tmp"), "TMP": str(root / "tmp"), "TEMP": str(root / "tmp"),
        "XDG_CACHE_HOME": str(root / "cache/xdg"),
        "XDG_CONFIG_HOME": str(root / "home/config"),
        "XDG_DATA_HOME": str(root / "home/data"),
        "HF_HOME": str(root / "cache/huggingface"),
        "HF_HUB_CACHE": str(root / "cache/huggingface/hub"),
        "HUGGINGFACE_HUB_CACHE": str(root / "cache/huggingface/hub"),
        "TRANSFORMERS_CACHE": str(root / "cache/transformers"),
        "TORCH_HOME": str(root / "cache/torch"),
        "MPLCONFIGDIR": str(root / "cache/matplotlib"),
        "NUMBA_CACHE_DIR": str(root / "cache/numba"),
        "PYTHONPYCACHEPREFIX": str(root / "cache/pycache"),
        "PYTHONNOUSERSITE": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_CACHE_DIR": str(root / "cache/pip"),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
        "PYANNOTE_METRICS_ENABLED": "0",
        "HF_HUB_OFFLINE": "1" if offline else "0",
    })
    return environment


def model_paths(root: Path) -> dict[str, Path]:
    manifest = validate_runtime(root)
    result = {}
    for kind in ("segmentation", "embedding"):
        entry = manifest.get("models", {}).get(kind)
        if not entry:
            raise ValueError(f"Missing downloaded {kind} model")
        path = Path(entry["checkpoint"])
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Invalid {kind} checkpoint path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
            raise ValueError(f"{kind} checkpoint hash does not match the runtime manifest")
        result[kind] = path
    return result
