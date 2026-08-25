import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from voxink.models import Source, TranscriptionOptions
from voxink.providers.qwen import QwenLocalProvider


class FakeModel:
    def __init__(self) -> None:
        self.calls = []

    def transcribe(self, **kwargs):
        self.calls.append(kwargs)
        return [SimpleNamespace(text="  Hello, world.  ", language="English")]


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

    async def test_requires_a_complete_local_model_directory(self) -> None:
        provider = QwenLocalProvider(Path(self.temp.name) / "missing")
        with self.assertRaises(FileNotFoundError):
            await provider.load()

    async def test_missing_audio_is_rejected_without_calling_model(self) -> None:
        provider = QwenLocalProvider(self.model_dir, model_loader=lambda *args: FakeModel())
        options = TranscriptionOptions("s", "g", "st", Source.SYSTEM)
        with self.assertRaises(FileNotFoundError):
            await provider.transcribe_file(Path(self.temp.name) / "missing.wav", options)

    async def test_streaming_explicitly_delegates_to_future_chunking_layer(self) -> None:
        provider = QwenLocalProvider(self.model_dir, model_loader=lambda *args: FakeModel())
        options = TranscriptionOptions("s", "g", "st", Source.SYSTEM)

        async def frames():
            yield b"data"

        with self.assertRaises(NotImplementedError):
            await provider.transcribe_stream(frames(), options).__anext__()


if __name__ == "__main__":
    unittest.main()
