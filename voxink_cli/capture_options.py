"""Normalize the public capture vocabulary before converting it to wire sources."""

from dataclasses import dataclass
from typing import Sequence

from .envelope import ErrorCode, Failure


@dataclass(frozen=True, slots=True)
class CaptureSelection:
    sources: tuple[str, ...]
    applications: tuple[str, ...]

    @property
    def wire_sources(self) -> list[str]:
        return ["system" if source == "applications" else source for source in self.sources]


def normalize_capture_selection(sources: Sequence[str], applications: Sequence[str]) -> CaptureSelection:
    selected = tuple(dict.fromkeys(sources))
    apps = tuple(dict.fromkeys(item.strip() for item in applications if item.strip()))
    if not selected or any(source not in {"microphone", "system", "applications"} for source in selected):
        raise Failure(ErrorCode.INVALID_REQUEST, "capture needs microphone, system, or applications sources")
    if "applications" in selected and not apps:
        raise Failure(ErrorCode.INVALID_REQUEST, "the applications source requires at least one application bundle identifier")
    if applications and "applications" not in selected:
        raise Failure(ErrorCode.INVALID_REQUEST, "application filters require the applications source")
    if "system" in selected and "applications" in selected:
        raise Failure(ErrorCode.INVALID_REQUEST, "choose all system audio or selected applications, not both")
    return CaptureSelection(selected, apps)
