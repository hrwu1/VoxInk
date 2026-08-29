"""Discover and validate offline transcription model packages."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


MODEL_MANIFEST = "voxink-model-manifest.json"


@dataclass(frozen=True, slots=True)
class LocalModelPackage:
    model_id: str
    display_name: str
    profile: str
    description: str
    provider: str
    path: Path
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ModelInspection:
    package: LocalModelPackage | None
    checked_files: int
    failures: tuple[str, ...]


def inspect_model_package(
    model_path: str | Path, *, verify_checksums: bool = False
) -> ModelInspection:
    """Inspect one package without resolving files outside its directory."""
    path = Path(model_path).expanduser().resolve()
    manifest_path = path / MODEL_MANIFEST
    if not path.is_dir():
        return ModelInspection(None, 0, (f"Local model directory is missing: {path}",))
    if not manifest_path.is_file():
        return ModelInspection(
            None,
            0,
            (f"Model manifest is missing: {manifest_path}. Restore the complete offline model package.",),
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest["files"]
        if manifest.get("schema_version") != 1 or not isinstance(files, list) or not files:
            raise ValueError("expected schema_version 1 and a non-empty files list")
        package = _package_from_manifest(path, manifest, files)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return ModelInspection(None, 0, (f"Model manifest is invalid: {exc}",))

    failures: list[str] = []
    checked = 0
    for entry in files:
        try:
            relative = _relative_file(entry)
            expected_size = int(entry["size"])
            expected_sha256 = str(entry["sha256"])
            file_path = path / relative
            if not file_path.is_file():
                failures.append(f"Model file is missing: {relative}")
                continue
            if file_path.stat().st_size != expected_size:
                failures.append(f"Model file has the wrong size: {relative}")
                continue
            if verify_checksums and _sha256(file_path) != expected_sha256:
                failures.append(f"Model file checksum does not match: {relative}")
                continue
            checked += 1
        except (KeyError, TypeError, ValueError, OSError) as exc:
            failures.append(f"Invalid model manifest entry: {exc}")
    return ModelInspection(package, checked, tuple(failures))


def discover_local_models(models_root: str | Path) -> tuple[LocalModelPackage, ...]:
    """Return complete immediate-child packages, without hashing large weights."""
    root = Path(models_root).expanduser().resolve()
    if not root.is_dir():
        return ()
    packages = []
    for candidate in root.iterdir():
        path = candidate.resolve()
        if path.parent != root or not path.is_dir():
            continue
        inspection = inspect_model_package(path)
        if inspection.package is not None and not inspection.failures:
            packages.append(inspection.package)
    return tuple(sorted(packages, key=lambda package: package.model_id))


def _package_from_manifest(
    path: Path, manifest: dict[str, Any], files: list[Any]
) -> LocalModelPackage:
    model_id = str(manifest.get("model") or path.name).strip()
    display_name = str(manifest.get("display_name") or model_id).strip()
    profile = str(manifest.get("profile") or "local").strip()
    description = str(manifest.get("description") or "Local transcription model").strip()
    provider = str(manifest.get("provider") or "qwen-local").strip()
    if not all((model_id, display_name, profile, description, provider)):
        raise ValueError("model metadata fields must not be empty")
    if model_id != path.name:
        raise ValueError("model must match its directory name")
    size_bytes = 0
    for entry in files:
        _relative_file(entry)
        size = int(entry["size"])
        if size < 0:
            raise ValueError("file size must not be negative")
        size_bytes += size
    return LocalModelPackage(
        model_id, display_name, profile, description, provider, path, size_bytes
    )


def _relative_file(entry: Any) -> Path:
    relative = Path(entry["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("path must remain inside the model directory")
    return relative


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
