"""Fail-fast checks for the local VoxInk developer workflow."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Callable, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_PATH = PROJECT_ROOT / "models" / "Qwen3-ASR-1.7B"
DEFAULT_OUTPUT_ROOT = Path.home() / "Documents" / "VoxInk"
DEFAULT_STATE_ROOT = Path.home() / "Library" / "Application Support" / "VoxInk"
MODEL_MANIFEST = "voxink-model-manifest.json"
MIN_FREE_BYTES = 512 * 1024 * 1024
MIN_AUDIO_FREE_BYTES = 2 * 1024 * 1024 * 1024

SERVICE_IMPORTS = ("fastapi", "uvicorn", "torch", "qwen_asr")
MICROPHONE_IMPORTS = ("sounddevice", "websockets")


class PreflightError(RuntimeError):
    """A set of actionable failures that must be fixed before startup."""

    def __init__(self, failures: Iterable[str]) -> None:
        self.failures = tuple(failures)
        super().__init__("\n".join(f"- {failure}" for failure in self.failures))


@dataclass(frozen=True, slots=True)
class PreflightOptions:
    model_path: Path = DEFAULT_MODEL_PATH
    output_root: Path = DEFAULT_OUTPUT_ROOT
    state_root: Path = DEFAULT_STATE_ROOT
    sources: tuple[str, ...] = ("microphone",)
    retain_audio: bool = False
    native_capture: Path | None = None
    verify_checksums: bool = True
    python_capture: bool = True


@dataclass(frozen=True, slots=True)
class PreflightReport:
    model_path: Path
    output_root: Path
    log_root: Path
    free_bytes: int
    checked_files: int


def run_preflight(
    options: PreflightOptions,
    *,
    import_available: Callable[[str], bool] | None = None,
) -> PreflightReport:
    """Validate a configured checkout, raising one combined actionable error."""
    failures: list[str] = []
    available = import_available or (lambda name: importlib.util.find_spec(name) is not None)

    if sys.version_info[:2] != (3, 12):
        failures.append(
            f"Python 3.12 is required; this command is using {sys.version.split()[0]}. "
            "Run it with .venv/bin/python."
        )

    required_imports = list(SERVICE_IMPORTS)
    if "microphone" in options.sources and options.python_capture:
        required_imports.extend(MICROPHONE_IMPORTS)
    missing = [name for name in required_imports if not available(name)]
    if missing:
        requirements = "requirements-capture.txt" if set(missing) & set(MICROPHONE_IMPORTS) else "requirements.txt"
        failures.append(
            f"Missing Python dependencies: {', '.join(missing)}. Install {requirements} "
            "in the active Python 3.12 environment."
        )

    checked_files = _check_model(options.model_path, options.verify_checksums, failures)
    output_root = options.output_root.expanduser().resolve()
    state_root = options.state_root.expanduser().resolve()
    log_root = state_root / "logs"
    for label, directory in (("transcript", output_root), ("diagnostic log", log_root)):
        _check_writable_directory(label, directory, failures)

    free_bytes = 0
    try:
        free_bytes = shutil.disk_usage(output_root).free
        required = MIN_AUDIO_FREE_BYTES if options.retain_audio else MIN_FREE_BYTES
        if free_bytes < required:
            failures.append(
                f"Only {_human_bytes(free_bytes)} is free at the transcript destination; "
                f"at least {_human_bytes(required)} is required"
                + (" when retaining audio." if options.retain_audio else ".")
            )
    except OSError as exc:
        failures.append(f"Cannot inspect free space at the transcript destination: {exc}")

    if any(source in {"system", "applications"} for source in options.sources):
        native = options.native_capture
        if native is None or not native.is_file() or not os.access(native, os.X_OK):
            failures.append(
                "System audio requires an executable native capture helper. "
                "Build clients/macos_capture and pass --native-capture PATH."
            )

    if failures:
        raise PreflightError(failures)
    return PreflightReport(options.model_path.resolve(), output_root, log_root, free_bytes, checked_files)


def _check_model(model_path: Path, verify_checksums: bool, failures: list[str]) -> int:
    model_path = model_path.expanduser().resolve()
    manifest_path = model_path / MODEL_MANIFEST
    if not model_path.is_dir():
        failures.append(f"Local model directory is missing: {model_path}")
        return 0
    if not manifest_path.is_file():
        failures.append(
            f"Model manifest is missing: {manifest_path}. Restore the complete offline model package."
        )
        return 0
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest["files"]
        if manifest.get("schema_version") != 1 or not isinstance(files, list) or not files:
            raise ValueError("expected schema_version 1 and a non-empty files list")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        failures.append(f"Model manifest is invalid: {exc}")
        return 0

    checked = 0
    for entry in files:
        try:
            relative = Path(entry["path"])
            expected_size = int(entry["size"])
            expected_sha256 = str(entry["sha256"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("path must remain inside the model directory")
            path = model_path / relative
            if not path.is_file():
                failures.append(f"Model file is missing: {relative}")
                continue
            if path.stat().st_size != expected_size:
                failures.append(f"Model file has the wrong size: {relative}")
                continue
            if verify_checksums and _sha256(path) != expected_sha256:
                failures.append(f"Model file checksum does not match: {relative}")
                continue
            checked += 1
        except (KeyError, TypeError, ValueError, OSError) as exc:
            failures.append(f"Invalid model manifest entry: {exc}")
    return checked


def _check_writable_directory(label: str, directory: Path, failures: list[str]) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".voxink-write-test-{os.getpid()}"
        probe.touch(exist_ok=False)
        probe.unlink()
    except OSError as exc:
        failures.append(f"The {label} directory is not writable ({directory}): {exc}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _human_bytes(value: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")
