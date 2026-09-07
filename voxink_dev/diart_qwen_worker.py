"""JSON-lines bridge to ONE Qwen model in VoxInk's existing environment.

Run only as a child of the experimental runner. Downloads and new model loads
per speaker are forbidden. Library output goes to stderr, IPC alone to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
import json
from pathlib import Path
import resource
import sys
import time


async def serve(args: argparse.Namespace) -> None:
    with redirect_stdout(sys.stderr):
        from voxink_core.local_models import inspect_model_package
        from voxink_core.models import Source, TranscriptionOptions
        from voxink_core.providers.qwen import QwenLocalProvider
        inspection = inspect_model_package(args.model, verify_checksums=True)
        if inspection.failures:
            raise RuntimeError("Qwen package verification failed: " + "; ".join(inspection.failures))
        provider = QwenLocalProvider(args.model, device=args.device)
        await provider.load()
    print(json.dumps({"ready": True, "model": str(args.model.resolve())}), flush=True)
    try:
        for line in sys.stdin:
            request = json.loads(line)
            started = time.monotonic()
            try:
                with redirect_stdout(sys.stderr):
                    event = await provider.transcribe_file(
                        Path(request["path"]),
                        TranscriptionOptions(
                            session_id=request["session_id"], segment_id=request["id"],
                            stream_id=request["stream_id"], source=Source(request["source"]),
                            start_ms=request["start_ms"], end_ms=request["end_ms"],
                            context=request.get("context"),
                        ),
                    )
                result = {"id": request["id"], "text": event.text, "language": event.language,
                          "asr_s": time.monotonic() - started,
                          "max_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
            except Exception as exc:
                result = {"id": request.get("id"), "error": f"{type(exc).__name__}: {exc}"}
            print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        with redirect_stdout(sys.stderr):
            await provider.unload()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    asyncio.run(serve(parser.parse_args()))


if __name__ == "__main__":
    main()
