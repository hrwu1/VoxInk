import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from voxink_core.models import Source, TranscriptionOptions
from voxink_core.providers.qwen import QwenLocalProvider


class FakeModel:
    def __init__(self) -> None:
        self.calls = []

    def transcribe(self, **kwargs):
        self.calls.append(kwargs)
        return [SimpleNamespace(text="  Hello, world.  ", language="English")]


class BlockingModel:
    """Record whether two transcriptions were ever inside the model at once."""

    def __init__(self, hold_seconds: float = 0.05) -> None:
        self.hold_seconds = hold_seconds
        self.concurrent = 0
        self.max_concurrent = 0

    def transcribe(self, **kwargs):
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            time.sleep(self.hold_seconds)
        finally:
            self.concurrent -= 1
        return [SimpleNamespace(text="ok", language="English")]


class QwenProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.model_dir = Path(self.temp.name) / "model"
        self.model_dir.mkdir()
        (self.model_dir / "config.json").write_text("{}")
        self.audio = Path(self.temp.name) / "audio.wav"
        self.audio.write_bytes(b"RIFFfake")

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_is_lazy_and_normalizes_fake_model_result(self) -> None:
        fake = FakeModel()
        loads = []

        def loader(path, device, tokens):
            loads.append((path, device, tokens))
            return fake

        provider = QwenLocalProvider(
            self.model_dir, device="cpu", max_new_tokens=42, model_loader=loader
        )
        self.assertFalse(provider.is_loaded)
        options = TranscriptionOptions(
            "session-1", "seg-1", "mic-1", Source.MICROPHONE,
            start_ms=10, end_ms=100, language="en",
        )
        result = await provider.transcribe_file(self.audio, options)

        self.assertTrue(provider.is_loaded)
        self.assertEqual(len(loads), 1)
        self.assertEqual(result.text, "Hello, world.")
        self.assertEqual(result.language, "en")
        self.assertTrue(result.is_final)
        self.assertEqual(fake.calls[0]["language"], "English")
        await provider.load()
        self.assertEqual(len(loads), 1)
        await provider.unload()
        self.assertFalse(provider.is_loaded)

    async def test_vocabulary_context_is_handed_to_the_model(self) -> None:
        fake = FakeModel()
        provider = QwenLocalProvider(self.model_dir, model_loader=lambda *args: fake)

        await provider.transcribe_file(
            self.audio,
            TranscriptionOptions(
                "s", "g", "mic-1", Source.MICROPHONE, context="VoxInk, Hrwu"
            ),
        )
        await provider.transcribe_file(
            self.audio, TranscriptionOptions("s", "g", "mic-1", Source.MICROPHONE)
        )

        self.assertEqual(fake.calls[0]["context"], "VoxInk, Hrwu")
        # Qwen rejects None; no vocabulary must mean an empty context.
        self.assertEqual(fake.calls[1]["context"], "")

    async def test_requires_a_complete_local_model_directory(self) -> None:
        provider = QwenLocalProvider(Path(self.temp.name) / "missing")
        with self.assertRaises(FileNotFoundError):
            await provider.load()

    async def test_missing_audio_is_rejected_without_calling_model(self) -> None:
        provider = QwenLocalProvider(self.model_dir, model_loader=lambda *args: FakeModel())
        options = TranscriptionOptions("s", "g", "st", Source.SYSTEM)
        with self.assertRaises(FileNotFoundError):
            await provider.transcribe_file(Path(self.temp.name) / "missing.wav", options)

    async def test_concurrent_transcriptions_never_overlap_in_the_model(self) -> None:
        # Microphone and system streams share one model built for a batch size
        # of one, so the provider must serialize inference itself.
        model = BlockingModel()
        provider = QwenLocalProvider(self.model_dir, model_loader=lambda *args: model)
        options = TranscriptionOptions("s", "g", "mic-1", Source.MICROPHONE)
        system_options = TranscriptionOptions("s", "g", "system-1", Source.SYSTEM)

        results = await asyncio.gather(
            provider.transcribe_file(self.audio, options),
            provider.transcribe_file(self.audio, system_options),
        )

        self.assertEqual(1, model.max_concurrent)
        self.assertEqual(["ok", "ok"], [result.text for result in results])

    async def test_unloading_between_load_and_inference_is_reported(self) -> None:
        provider = QwenLocalProvider(self.model_dir, model_loader=lambda *args: FakeModel())
        await provider.load()
        await provider.unload()
        provider._model = None  # simulate an unload racing an in-flight request
        options = TranscriptionOptions("s", "g", "st", Source.SYSTEM)
        original_load = provider.load

        async def load_then_drop() -> None:
            await original_load()
            provider._model = None

        provider.load = load_then_drop  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            await provider.transcribe_file(self.audio, options)

    async def test_streaming_explicitly_delegates_to_future_chunking_layer(self) -> None:
        provider = QwenLocalProvider(self.model_dir, model_loader=lambda *args: FakeModel())
        options = TranscriptionOptions("s", "g", "st", Source.SYSTEM)

        async def frames():
            yield b"data"

        with self.assertRaises(NotImplementedError):
            await provider.transcribe_stream(frames(), options).__anext__()


if __name__ == "__main__":
    unittest.main()
