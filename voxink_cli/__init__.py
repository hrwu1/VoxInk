"""The public `voxink` command and the typed client it shares with adapters.

`PROJECT_SPEC.md` §14 defines this as a thin client of VoxInk's local control
plane. Nothing here implements capture, transcription, persistence, model
management, or recovery; every product capability is reached through
`ControlClient`, which future MCP or SDK adapters call directly instead of
shelling out and parsing terminal text.

`voxink shell` is the same command as an interactive Terminal interface.
"""

from .envelope import SCHEMA_VERSION, ErrorCode, Failure

__all__ = ["SCHEMA_VERSION", "ErrorCode", "Failure"]
