"""The isolated diarization worker.

Run as `python -m voxink_core.diarization.worker`, reading one JSON request on
stdin and writing one JSON response on stdout. It exists as a separate process
so that a segmentation fault, a runaway allocation, or a Torch abort takes down
this process and nothing else: the transcription service keeps serving and the
session's canonical artifacts are never in the blast radius.

A typed failure is a *result*, not a crash, so it leaves through stdout with a
zero exit status. A non-zero exit means the process genuinely died, and the
supervisor reports `worker_crashed`.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

# Imported before pyannote so the offline environment is in place first.
from .pyannote import PyannoteCommunityProvider, apply_offline_environment
from .aligner import QwenForcedAligner
from .models import (
    AlignmentBlock,
    DiarizationError,
    DiarizationErrorCode,
    DiarizationRequest,
    DiarizationResult,
)
from ..models import Source, WordTimestamp


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Diarize one request, returning a JSON-serializable response."""
    apply_offline_environment()
    try:
        request = DiarizationRequest(
            session_id=payload["session_id"],
            source=Source(payload["source"]),
            audio_path=Path(payload["audio_path"]),
            model_path=Path(payload["model_path"]),
            min_speakers=payload.get("min_speakers"),
            max_speakers=payload.get("max_speakers"),
        )
        provider = PyannoteCommunityProvider(device=payload.get("device", "cpu"))
        response: dict[str, Any] = {"ok": True, "result": encode_result(provider.diarize(request))}
    except DiarizationError as exc:
        return {"ok": False, "error": exc.to_dict()}
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "ok": False,
            "error": DiarizationError(
                DiarizationErrorCode.INVALID_AUDIO, f"{type(exc).__name__}: {exc}"
            ).to_dict(),
        }
    alignment = payload.get("alignment")
    if alignment:
        # Diarization succeeded; the pipeline is released before the aligner is
        # loaded so the two never have to fit in memory together.
        del provider
        response["alignment"] = _align(alignment, request.audio_path, payload.get("device", "cpu"))
    return response


def _align(alignment: dict[str, Any], audio_path: Path, device: str) -> dict[str, Any]:
    """Run the best-effort second stage: word timings for every block sent.

    Nothing raised here reaches the caller as a failure. Alignment refines
    labels; a session without it is labeled the way it was before alignment
    existed, and the response says why.
    """
    try:
        blocks = [AlignmentBlock(**item) for item in alignment["blocks"]]
        aligner = QwenForcedAligner(device=device)
        model = aligner.load(Path(alignment["model_path"]))
        timings = aligner.align(audio_path, blocks)
    except DiarizationError as exc:
        return {"ok": False, "error": exc.to_dict()}
    except Exception as exc:  # noqa: BLE001 - a crashed stage, not a crashed job
        return {
            "ok": False,
            "error": DiarizationError(
                DiarizationErrorCode.WORKER_CRASHED, f"{type(exc).__name__}: {exc}"
            ).to_dict(),
        }
    return {
        "ok": True,
        "model_id": model.source_model_id,
        "model_revision": model.revision,
        "device": aligner.resolved_device,
        "words": {
            block_id: [
                {"text": word.text, "start_ms": word.start_ms, "end_ms": word.end_ms}
                for word in words
            ]
            for block_id, words in timings.items()
        },
    }


def decode_alignment(payload: dict[str, Any] | None) -> dict[str, tuple[WordTimestamp, ...]]:
    """Rebuild the aligned words in the parent, re-validating each one."""
    if not payload or not payload.get("ok"):
        return {}
    return {
        str(block_id): tuple(
            WordTimestamp(
                text=str(item["text"]),
                start_ms=int(item["start_ms"]),
                end_ms=int(item["end_ms"]),
            )
            for item in words
        )
        for block_id, words in payload.get("words", {}).items()
    }


def encode_result(result: DiarizationResult) -> dict[str, Any]:
    return {
        "model": result.model,
        "model_revision": result.model_revision,
        "raw_turns": [_encode_turn(turn) for turn in result.raw_turns],
        "exclusive_turns": [_encode_turn(turn) for turn in result.exclusive_turns],
    }


def decode_result(payload: dict[str, Any], *, source: Source) -> DiarizationResult:
    """Rebuild a result in the parent, re-validating everything it carries."""
    from .models import SpeakerTurn

    def turns(key: str, exclusive: bool) -> tuple[SpeakerTurn, ...]:
        return tuple(
            SpeakerTurn(
                speaker_id=item["speaker_id"],
                start_ms=int(item["start_ms"]),
                end_ms=int(item["end_ms"]),
                source=source,
                exclusive=exclusive,
            )
            for item in payload[key]
        )

    return DiarizationResult(
        model=payload["model"],
        model_revision=payload["model_revision"],
        raw_turns=turns("raw_turns", False),
        exclusive_turns=turns("exclusive_turns", True),
    )


def _encode_turn(turn) -> dict[str, Any]:
    return {
        "speaker_id": turn.speaker_id,
        "start_ms": turn.start_ms,
        "end_ms": turn.end_ms,
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        json.dump(
            {
                "ok": False,
                "error": DiarizationError(
                    DiarizationErrorCode.WORKER_CRASHED, f"unreadable request: {exc}"
                ).to_dict(),
            },
            sys.stdout,
        )
        return 0
    json.dump(run(payload), sys.stdout)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
