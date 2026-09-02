"""A local, opt-in registry of voices the user chose to remember.

Diarization labels speakers anonymously and forgets them when the session
ends. When the user names a speaker and asks VoxInk to remember the voice, the
speaker's embedding is stored here under that name, and the next session's
anonymous speakers are compared against it. Everything stays in one 0600 file
on this Mac; nothing is ever sent anywhere, and forgetting a voice deletes it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import secrets
import tempfile
import threading
from typing import Any, Iterable, Sequence


SPEAKERS_FILENAME = "speakers.json"
SCHEMA_VERSION = 1
#: Cosine similarity a session speaker must reach to be called a known voice.
#: Deliberately strict: a wrong name on a transcript is worse than "Speaker 2".
DEFAULT_THRESHOLD = 0.62


class SpeakerRegistryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class KnownSpeaker:
    id: str
    name: str
    #: Mean of every enrolled embedding, unit-normalized.
    embedding: tuple[float, ...]
    model_id: str
    samples: int
    created_at: str
    updated_at: str

    def to_dict(self, *, include_embedding: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if include_embedding:
            data["embedding"] = list(self.embedding)
        else:
            data.pop("embedding")
            data["dimension"] = len(self.embedding)
        return data


@dataclass(frozen=True, slots=True)
class SpeakerMatch:
    speaker: KnownSpeaker
    similarity: float


class SpeakerRegistry:
    def __init__(self, path: str | Path, *, threshold: float = DEFAULT_THRESHOLD) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must fall in (0, 1]")
        self.path = Path(path).expanduser()
        self.threshold = threshold
        self._lock = threading.RLock()

    def list(self) -> list[KnownSpeaker]:
        with self._lock:
            return self._read()

    def get(self, speaker_id: str) -> KnownSpeaker | None:
        return next((item for item in self.list() if item.id == speaker_id), None)

    def find_by_name(self, name: str) -> KnownSpeaker | None:
        wanted = _clean_name(name).casefold()
        return next((item for item in self.list() if item.name.casefold() == wanted), None)

    def match(self, embedding: Sequence[float], *, model_id: str | None = None) -> SpeakerMatch | None:
        """The best known voice above the threshold, or None."""
        vector = _unit(embedding)
        if vector is None:
            return None
        best: SpeakerMatch | None = None
        for known in self.list():
            if model_id and known.model_id != model_id:
                # Embeddings from different models do not live in one space.
                continue
            if len(known.embedding) != len(vector):
                continue
            similarity = cosine(vector, known.embedding)
            if similarity >= self.threshold and (best is None or similarity > best.similarity):
                best = SpeakerMatch(known, similarity)
        return best

    def enroll(self, name: str, embedding: Sequence[float], *, model_id: str) -> KnownSpeaker:
        """Remember a voice under a name; an existing name absorbs the new sample."""
        cleaned = _clean_name(name)
        if not cleaned:
            raise SpeakerRegistryError("a speaker needs a name")
        vector = _unit(embedding)
        if vector is None:
            raise SpeakerRegistryError("embedding must be a finite, non-zero vector")
        now = _now()
        with self._lock:
            speakers = self._read()
            for index, known in enumerate(speakers):
                if known.name.casefold() == cleaned.casefold() and known.model_id == model_id:
                    if len(known.embedding) != len(vector):
                        raise SpeakerRegistryError("embedding size differs from the stored voice")
                    merged = _unit(
                        tuple(
                            stored * known.samples + fresh
                            for stored, fresh in zip(known.embedding, vector)
                        )
                    )
                    updated = replace(
                        known, embedding=merged or vector, samples=known.samples + 1, updated_at=now
                    )
                    speakers[index] = updated
                    self._write(speakers)
                    return updated
            created = KnownSpeaker(
                id=f"spk_{secrets.token_hex(5)}",
                name=cleaned,
                embedding=vector,
                model_id=model_id,
                samples=1,
                created_at=now,
                updated_at=now,
            )
            speakers.append(created)
            self._write(speakers)
            return created

    def rename(self, speaker_id: str, name: str) -> KnownSpeaker:
        cleaned = _clean_name(name)
        if not cleaned:
            raise SpeakerRegistryError("a speaker needs a name")
        with self._lock:
            speakers = self._read()
            for index, known in enumerate(speakers):
                if known.id == speaker_id:
                    updated = replace(known, name=cleaned, updated_at=_now())
                    speakers[index] = updated
                    self._write(speakers)
                    return updated
        raise SpeakerRegistryError("unknown speaker")

    def forget(self, speaker_id: str) -> KnownSpeaker:
        with self._lock:
            speakers = self._read()
            for index, known in enumerate(speakers):
                if known.id == speaker_id:
                    del speakers[index]
                    self._write(speakers)
                    return known
        raise SpeakerRegistryError("unknown speaker")

    # -- storage ----------------------------------------------------------------

    def _read(self) -> list[KnownSpeaker]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            raise SpeakerRegistryError(f"speaker registry is unreadable: {exc}") from exc
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise SpeakerRegistryError("speaker registry uses an unsupported format")
        speakers = []
        for item in raw.get("speakers", []):
            try:
                speakers.append(
                    KnownSpeaker(
                        id=str(item["id"]),
                        name=str(item["name"]),
                        embedding=tuple(float(value) for value in item["embedding"]),
                        model_id=str(item.get("model_id", "")),
                        samples=int(item.get("samples", 1)),
                        created_at=str(item.get("created_at", "")),
                        updated_at=str(item.get("updated_at", "")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return speakers

    def _write(self, speakers: Iterable[KnownSpeaker]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "speakers": [item.to_dict(include_embedding=True) for item in speakers],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(dir=self.path.parent, prefix=".speakers-", suffix=".json")
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
            temporary.chmod(0o600)
            temporary.replace(self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def _unit(values: Sequence[float]) -> tuple[float, ...] | None:
    try:
        vector = tuple(float(value) for value in values)
    except (TypeError, ValueError):
        return None
    if not vector or any(not math.isfinite(value) for value in vector):
        return None
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return None
    return tuple(value / norm for value in vector)


def _clean_name(name: str) -> str:
    return " ".join(str(name).split())[:80]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
