"""Create, install, inspect, or remove the disposable Diart experiment runtime.

The runtime has its own Python environment, models, home, temporary files and
caches. Its absolute path survives a worktree removal. Cleanup is a dry run
unless --delete is supplied, and only accepts an exact ownership-marked root.
Serving and worker code import the shared core runtime contract instead of this
developer setup entry point; its existing helper imports remain compatible.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from voxink_core.diarization.diart_runtime import (
    DEFAULT_RUNTIME,
    DIRECTORIES,
    MARKER,
    OWNER,
    model_paths,
    record_action,
    runtime_environment,
    validate_runtime,
    validate_runtime_path as _path,
)


MODEL_REVISIONS = {
    "segmentation": ("pyannote/segmentation-3.0", "e66f3d3b9eb0873085418a7b813d3b369bf160bb"),
    "embedding": ("pyannote/embedding", "4db4899737a38b2d618bbd74350915aa10293cb2"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize(root: Path, *, python: Path) -> dict:
    root = _path(root)
    if root.exists():
        raise ValueError("Runtime already exists; refusing to claim pre-existing files")
    root.mkdir(parents=True)
    identity = {"owner": OWNER, "runtime_root": str(root), "identity": str(uuid.uuid4())}
    (root / MARKER).write_text(json.dumps(identity, indent=2) + "\n")
    manifest = {
        **identity,
        "created_at": _now(),
        "python_source": str(python),
        "created_paths": [str(root / name) for name in (*DIRECTORIES, MARKER, "manifest.json", "INSTALL-ACTIONS.jsonl")],
        "models": {},
        "cleanup": f"python3 -m voxink_dev.diart_runtime cleanup --runtime {root} --delete",
        "ownership": "Every descendant is experiment-owned; no existing runtime or cache was adopted.",
    }
    for name in DIRECTORIES:
        if name != ".venv":
            (root / name).mkdir()
    save_manifest(root, manifest)
    record_action(root, "initialize", python=str(python))
    return manifest


def save_manifest(root: Path, manifest: dict) -> None:
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def run_action(root: Path, action: str, argv: list[str], *, offline: bool = True) -> None:
    validate_runtime(root)
    log = root / "logs" / f"{action}.log"
    if log.exists():
        archived = log.with_name(f"{action}-{uuid.uuid4().hex[:8]}.log")
        log.rename(archived)
        record_action(root, "archive-log", previous=str(log), archived=str(archived))
    record_action(root, action, argv=argv, log=str(log), status="started")
    with log.open("w") as output:
        result = subprocess.run(argv, cwd=root, env=runtime_environment(root, offline=offline), stdout=output, stderr=subprocess.STDOUT)
    record_action(root, action, returncode=result.returncode, status="finished")
    if result.returncode:
        raise RuntimeError(f"{action} failed with exit {result.returncode}; see {log}")


def install(root: Path, requirements: Path, python: Path) -> None:
    validate_runtime(root)
    if not (root / ".venv").exists():
        run_action(root, "create-venv", [str(python), "-m", "venv", str(root / ".venv")])
    local_requirements = root / "requirements-input.txt"
    for name in ("requirements-input.txt", "requirements-lock.txt", "pip-install-report.json"):
        path = root / name
        if path.exists():
            archived = root / "logs" / f"{uuid.uuid4().hex[:8]}-{name}"
            shutil.copyfile(path, archived)
            record_action(root, "archive-install-metadata", previous=str(path), archived=str(archived))
    shutil.copyfile(requirements, local_requirements)
    executable = str(root / ".venv/bin/python")
    run_action(root, "pip-install", [executable, "-m", "pip", "install", "--report", str(root / "pip-install-report.json"), "-r", str(local_requirements)], offline=False)
    run_action(root, "pip-check", [executable, "-m", "pip", "check"])
    run_action(root, "pip-freeze", [executable, "-m", "pip", "freeze", "--all"])
    shutil.copyfile(root / "logs/pip-freeze.log", root / "requirements-lock.txt")
    manifest = validate_runtime(root)
    manifest["dependencies"] = {"input": str(local_requirements), "lock": str(root / "requirements-lock.txt"), "pip_report": str(root / "pip-install-report.json"), "checked_at": _now()}
    manifest["created_paths"] = sorted(str(path) for path in root.iterdir())
    save_manifest(root, manifest)


def adapt_community_models(root: Path, source: Path) -> None:
    """Verify and adapt owned copies, retaining exactly the existing tensors.

    Community-1 omits a historical Torch-version metadata field required by
    pyannote 3.3. This fills that field only in a new checkpoint. The inserted
    runtime version is a compatibility sentinel, not a claim about training.
    """
    validate_runtime(root)
    if Path(sys.prefix).resolve() != (root / ".venv").resolve():
        raise ValueError("Run adapt-community with this runtime's .venv/bin/python")
    environment = runtime_environment(root)
    os.environ.clear()
    os.environ.update(environment)
    import torch
    from diart.models import SegmentationModel, EmbeddingModel

    source_manifest = json.loads((source / "voxink-model-manifest.json").read_text())
    source_files = {entry["path"]: entry for entry in source_manifest["files"]}
    if source_manifest["source_model_id"] != "pyannote/speaker-diarization-community-1":
        raise ValueError("Expected a manifested local Community-1 model")
    manifest = validate_runtime(root)
    for kind, cls in (("segmentation", SegmentationModel), ("embedding", EmbeddingModel)):
        relative = f"{kind}/pytorch_model.bin"
        original = source / relative
        expected = source_files[relative]["sha256"]
        before = hashlib.sha256(original.read_bytes()).hexdigest()
        if before != expected:
            raise ValueError(f"Existing {kind} checkpoint does not match its source manifest")
        checkpoint = torch.load(original, map_location="cpu")
        versions = checkpoint["pyannote.audio"]["versions"]
        if "torch" in versions:
            raise ValueError("Source checkpoint already has Torch metadata; adaptation does not apply")
        versions["torch"] = str(torch.__version__)
        destination = root / "models" / f"community-1-{kind}-compat.bin"
        if destination.is_symlink():
            raise ValueError("Adapted checkpoint destination must not be a symlink")
        record_action(root, "adapt-community-model", model_id=source_manifest["source_model_id"], kind=kind, source=str(original), destination=str(destination), source_sha256=before, status="started")
        torch.save(checkpoint, destination)
        reloaded = torch.load(destination, map_location="cpu")
        if checkpoint["state_dict"].keys() != reloaded["state_dict"].keys() or any(
            not torch.equal(value, reloaded["state_dict"][key])
            for key, value in checkpoint["state_dict"].items()
        ):
            raise RuntimeError("Checkpoint adaptation changed model tensors")
        model = cls.from_pretrained(str(destination), use_hf_token=False).eval()
        torch.manual_seed(0)
        audio = torch.randn(1, 1, 80_000) * 0.01
        with torch.inference_mode():
            output = model(audio) if kind == "segmentation" else model(audio, torch.ones(1, 293))
        if not torch.isfinite(output).all():
            raise RuntimeError(f"{kind} forward pass produced non-finite values")
        if hashlib.sha256(original.read_bytes()).hexdigest() != before:
            raise RuntimeError("Read-only source checkpoint changed during adaptation")
        manifest["models"][kind] = {
            "model_id": source_manifest["source_model_id"], "component": kind,
            "revision": source_manifest["revision"], "license": source_manifest["license"],
            "checkpoint": str(destination), "bytes": destination.stat().st_size,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "source_checkpoint_read_only": str(original), "source_sha256": before,
            "adaptation": {"added_metadata": {"pyannote.audio.versions.torch": str(torch.__version__)}, "meaning": "Loader compatibility sentinel; source training Torch version is unknown", "tensors_exactly_equal": True},
            "validation": {"device": "cpu", "samples": 80000, "sample_rate": 16000, "shape": list(output.shape), "finite": True, "weighted_embedding": kind == "embedding", "verified_at": _now()},
        }
        save_manifest(root, manifest)
        record_action(root, "adapt-community-model", kind=kind, status="finished", shape=list(output.shape), tensors_exactly_equal=True)
        print(json.dumps({"model": kind, "checkpoint": str(destination), "shape": list(output.shape), "finite": True}), flush=True)


def download_models(root: Path, token_path: Path | None) -> None:
    """Run inside the isolated venv; token is read into memory and never saved."""
    validate_runtime(root)
    environment = runtime_environment(root, offline=False)
    os.environ.clear()
    os.environ.update(environment)
    from huggingface_hub import HfApi, snapshot_download

    token = token_path.read_text().strip() if token_path and token_path.is_file() else False
    api = HfApi(token=token)
    errors = []
    for kind, (repo, revision) in MODEL_REVISIONS.items():
        info = api.model_info(repo, revision=revision)
        directory = root / "models" / repo.replace("/", "--")
        manifest = validate_runtime(root)
        attempt = {"model_id": repo, "revision": revision, "license": (info.card_data or {}).get("license", "unknown"), "destination": str(directory), "status": "started", "attempted_at": _now()}
        manifest.setdefault("model_download_attempts", {})[kind] = attempt
        save_manifest(root, manifest)
        record_action(root, "model-download", model_id=repo, revision=revision, destination=str(directory), status="started")
        try:
            snapshot_download(repo, revision=revision, local_dir=directory, cache_dir=root / "cache/huggingface/hub", allow_patterns=["pytorch_model.bin", "config.yaml", "README.md", "LICENSE*"], token=token)
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            record_action(root, "model-download", model_id=repo, revision=revision, status="failed", error=type(exc).__name__, http_status=status_code)
            attempt.update(status="failed", error=type(exc).__name__, http_status=status_code)
            manifest["model_download_attempts"][kind] = attempt
            save_manifest(root, manifest)
            errors.append(f"{repo}: {type(exc).__name__}, HTTP {status_code}")
            continue
        checkpoint = directory / "pytorch_model.bin"
        if not checkpoint.is_file():
            raise RuntimeError(f"No checkpoint in {directory}")
        card = info.card_data or {}
        manifest = validate_runtime(root)
        manifest["model_download_attempts"][kind]["status"] = "downloaded"
        manifest["models"][kind] = {
            "model_id": repo, "revision": revision, "license": card.get("license", "unknown"),
            "checkpoint": str(checkpoint), "bytes": checkpoint.stat().st_size,
            "sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
            "downloaded_at": _now(), "source": f"https://huggingface.co/{repo}/tree/{revision}",
        }
        save_manifest(root, manifest)
        record_action(root, "model-download", model_id=repo, revision=revision, status="finished")
    if errors:
        raise RuntimeError("Model download failed: " + "; ".join(errors))


def cleanup(root: Path, *, delete: bool = False) -> dict:
    manifest = validate_runtime(root)
    result = {"runtime_root": str(root), "identity": manifest["identity"], "delete": delete, "paths": sorted(str(path) for path in root.iterdir())}
    if delete:
        shutil.rmtree(root)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("action", choices=("init", "install", "download", "adapt-community", "inspect", "cleanup"))
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--python", type=Path, default=Path("/opt/homebrew/bin/python3.12"))
    parser.add_argument("--requirements", type=Path, default=Path(__file__).resolve().parents[1] / "requirements-diart-experiment.txt")
    parser.add_argument("--token-path", type=Path, default=None, help="Read an existing HF token file without copying it")
    parser.add_argument("--source-model", type=Path, help="Existing manifested Community-1 directory, read only")
    parser.add_argument("--delete", action="store_true", help="Actually delete the ownership-marked runtime")
    args = parser.parse_args()
    root = _path(args.runtime)
    if args.action == "init":
        initialize(root, python=args.python)
    elif args.action == "install":
        install(root, args.requirements, args.python)
    elif args.action == "download":
        download_models(root, args.token_path)
    elif args.action == "adapt-community":
        if args.source_model is None:
            parser.error("adapt-community requires --source-model")
        adapt_community_models(root, args.source_model)
    elif args.action == "cleanup":
        print(json.dumps(cleanup(root, delete=args.delete), indent=2))
        return
    print(json.dumps(validate_runtime(root), indent=2))


if __name__ == "__main__":
    main()
