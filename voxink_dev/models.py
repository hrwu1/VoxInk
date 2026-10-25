"""Download VoxInk's local models from their publishers.

VoxInk never ships model weights. This command fetches each model directly
from its official Hugging Face repository at the revision recorded in the
tracked `voxink-model-manifest.json`, after showing its license and asking,
and then verifies every file against that manifest. It is the only VoxInk
command that uses the network; the service itself stays offline.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import sys
from typing import Callable

from voxink_core.local_models import MODEL_MANIFEST, inspect_model_package


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODELS_ROOT = PROJECT_ROOT / "models"
DEFAULT_MODEL = "Qwen3-ASR-1.7B"


class DownloadError(Exception):
    """A file could not be fetched; the message says what the user can do."""


@dataclass(frozen=True, slots=True)
class ModelSource:
    name: str
    display_name: str
    description: str
    repo_id: str
    revision: str
    license: str
    gated: bool
    files: tuple[str, ...]
    size_bytes: int
    path: Path

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repo_id}"

    def installed(self) -> bool:
        return not inspect_model_package(self.path).failures


Fetch = Callable[[ModelSource, str], None]
Ask = Callable[[str], str]


def read_sources(models_root: Path) -> tuple[ModelSource, ...]:
    """Every tracked manifest that names where its weights come from."""
    sources = []
    for manifest_path in sorted(models_root.glob(f"*/{MODEL_MANIFEST}")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not manifest.get("source_model_id") or not manifest.get("revision"):
            continue
        files = manifest["files"]
        sources.append(
            ModelSource(
                name=manifest_path.parent.name,
                display_name=str(manifest.get("display_name") or manifest_path.parent.name),
                description=str(manifest.get("description") or ""),
                repo_id=str(manifest["source_model_id"]),
                revision=str(manifest["revision"]),
                license=str(manifest.get("license") or "see the model card"),
                gated=bool(manifest.get("gated", False)),
                files=tuple(str(entry["path"]) for entry in files),
                size_bytes=sum(int(entry["size"]) for entry in files),
                path=manifest_path.parent,
            )
        )
    return tuple(sources)


def hub_fetch(source: ModelSource, filename: str) -> None:
    from huggingface_hub import hf_hub_download  # noqa: PLC0415
    from huggingface_hub.errors import GatedRepoError, HfHubHTTPError, LocalEntryNotFoundError  # noqa: PLC0415

    try:
        hf_hub_download(source.repo_id, filename, revision=source.revision, local_dir=source.path)
    except GatedRepoError as exc:
        raise DownloadError(
            f"{source.repo_id} is gated. Accept its conditions at {source.url} while signed in, "
            "then sign in here with: .venv/bin/hf auth login"
        ) from exc
    except LocalEntryNotFoundError as exc:
        raise DownloadError(
            f"Could not reach Hugging Face for {filename}. Check the network connection, "
            "and unset HF_HUB_OFFLINE if it is set."
        ) from exc
    except (HfHubHTTPError, OSError) as exc:
        raise DownloadError(f"{filename}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voxink-models", description=__doc__.split("\n")[0])
    parser.add_argument("--models-root", type=Path, default=DEFAULT_MODELS_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="show every model, its license, and whether it is installed")
    download = commands.add_parser("download", help=f"download models (default: {DEFAULT_MODEL})")
    download.add_argument("models", nargs="*", metavar="MODEL")
    download.add_argument("--all", action="store_true", help="download every model listed by `list`")
    download.add_argument("--yes", action="store_true", help="accept the licenses shown without asking")
    return parser


def _gib(size: int) -> str:
    return f"{size / 1024**3:.2f} GiB"


def list_models(sources: tuple[ModelSource, ...]) -> int:
    for source in sources:
        state = "installed" if source.installed() else "not installed"
        gated = ", gated" if source.gated else ""
        print(f"{source.name:42} {_gib(source.size_bytes):>9}  {source.license}{gated}  [{state}]")
        print(f"{'':42} {source.description}")
    print(f"\nDownload with: ./scripts/voxink-models download MODEL (default: {DEFAULT_MODEL})")
    return 0


def download_models(
    sources: tuple[ModelSource, ...], args: argparse.Namespace, *, fetch: Fetch, ask: Ask, interactive: bool
) -> int:
    by_name = {source.name: source for source in sources}
    names = list(by_name) if args.all else (args.models or [DEFAULT_MODEL])
    unknown = [name for name in names if name not in by_name]
    if unknown:
        print(f"Unknown model: {', '.join(unknown)}. Available: {', '.join(by_name)}", file=sys.stderr)
        return 2
    wanted = [by_name[name] for name in dict.fromkeys(names)]
    for source in wanted:
        if source.installed():
            print(f"{source.name} is already installed.")
    pending = [source for source in wanted if not source.installed()]
    if not pending:
        return 0

    print("\nThese models are published by third parties. They are not part of VoxInk and are not")
    print("covered by its MIT license; each is downloaded from its publisher under its own license.\n")
    for source in pending:
        print(f"  {source.display_name} ({_gib(source.size_bytes)})")
        print(f"    from {source.url} at revision {source.revision[:12]}")
        print(f"    license: {source.license} (see {source.url})")
        if source.gated:
            print(f"    gated: accept its conditions at {source.url} while signed in to Hugging Face,")
            print("    then sign in here once with: .venv/bin/hf auth login")
    total = sum(source.size_bytes for source in pending)
    free = shutil.disk_usage(pending[0].path).free
    if free < total:
        print(f"\nNot enough disk space: {_gib(total)} needed, {_gib(free)} free.", file=sys.stderr)
        return 1
    if not args.yes:
        if not interactive:
            print("\nRe-run with --yes to accept these licenses without a prompt.", file=sys.stderr)
            return 2
        if ask(f"\nDownload {_gib(total)} and accept these licenses? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing downloaded.")
            return 1

    failed = 0
    for source in pending:
        print(f"\nDownloading {source.name}...", flush=True)
        try:
            for filename in source.files:
                fetch(source, filename)
        except DownloadError as exc:
            print(f"{source.name} was not downloaded: {exc}", file=sys.stderr)
            failed += 1
            continue
        inspection = inspect_model_package(source.path, verify_checksums=True)
        if inspection.failures:
            print(f"{source.name} does not match its manifest:", file=sys.stderr)
            for failure in inspection.failures:
                print(f"  - {failure}", file=sys.stderr)
            failed += 1
            continue
        print(f"{source.name} is installed and verified ({inspection.checked_files} files).")
    return 1 if failed else 0


def main(argv: list[str] | None = None, *, fetch: Fetch = hub_fetch, ask: Ask = input) -> int:
    args = build_parser().parse_args(argv)
    sources = read_sources(args.models_root.expanduser().resolve())
    if args.command == "list":
        return list_models(sources)
    return download_models(sources, args, fetch=fetch, ask=ask, interactive=sys.stdin.isatty())


if __name__ == "__main__":
    raise SystemExit(main())
