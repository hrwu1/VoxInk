"""Subprocess fixture implementing the real helper contract without loading Qwen."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from services.transcription.app import ServiceSettings, create_app
from voxink_core.models import TranscriptEvent


class FakeProvider:
    name = "fake-local"
    is_loaded = True

    async def load(self) -> None:
        self.is_loaded = True

    async def transcribe_file(self, audio, options):
        return TranscriptEvent(
            session_id=options.session_id,
            segment_id=options.segment_id,
            stream_id=options.stream_id,
            source=options.source,
            start_ms=options.start_ms,
            end_ms=options.end_ms,
            language=options.language or "en",
            text="Supervised microphone transcription works.",
            is_final=True,
        )

    async def transcribe_stream(self, frames, options):
        raise NotImplementedError
        yield

    async def unload(self) -> None:
        self.is_loaded = False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--device")
    args = parser.parse_args()
    del args.model, args.device
    secret = os.environ.get("VOXINK_AUTH_SECRET")
    if not secret:
        parser.error("VOXINK_AUTH_SECRET is required")
    app = create_app(
        ServiceSettings(output_root=args.output, auth_secret=secret),
        provider=FakeProvider(),
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
