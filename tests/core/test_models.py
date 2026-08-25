import unittest

from voxink.models import SessionConfig, Source, TranscriptEvent, WordTimestamp


class ModelTests(unittest.TestCase):
    def test_event_round_trip_preserves_nested_words(self) -> None:
        event = TranscriptEvent(
            session_id="session-1",
            segment_id="seg-1",
            stream_id="mic-1",
            source=Source.MICROPHONE,
            start_ms=1200,
            end_ms=2400,
            text="Hello.",
            is_final=True,
            language="en",
            words=(WordTimestamp("Hello", 1200, 2300, 0.9),),
            diagnostics={"provider_latency_ms": 50},
        )

        encoded = event.to_dict()
        self.assertEqual(encoded["source"], "microphone")
        self.assertNotIn("diagnostics", encoded)
        self.assertEqual(TranscriptEvent.from_dict(encoded), event)

    def test_event_accepts_wire_source_value(self) -> None:
        event = TranscriptEvent("s", "g", "st", "system", 0, 1, "hi", False)
        self.assertIs(event.source, Source.SYSTEM)

    def test_invalid_time_range_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TranscriptEvent("s", "g", "st", Source.SYSTEM, 2, 1, "", True)

    def test_session_requires_unique_sources(self) -> None:
        with self.assertRaises(ValueError):
            SessionConfig("s", (Source.SYSTEM, Source.SYSTEM))

    def test_session_id_cannot_escape_output_root(self) -> None:
        for value in ("../escape", "..", ".", "bad\\path"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SessionConfig(value, (Source.SYSTEM,))


if __name__ == "__main__":
    unittest.main()
