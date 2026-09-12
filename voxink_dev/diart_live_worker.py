"""Compatibility entry point for the core-owned offline Diart worker.

Existing experiment commands may still use ``python -m voxink_dev.diart_live_worker``.
The service launches ``voxink_core.diarization.diart_live_worker`` directly.
"""

from voxink_core.diarization.diart_live_worker import (
    CONTEXT_PAD_MS,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    RATE,
    RING_BYTES,
    SILENCE_RMS_DBFS,
    STEP,
    STEP_BYTES,
    DiartPredictor,
    StreamingTurns,
    main,
    rms_dbfs,
    serve,
    validate_pcm,
)


if __name__ == "__main__":
    main()
