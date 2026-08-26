import unittest

from voxink_core.models import Source, TranscriptEvent
from voxink_core.transcripts import OverlapDeduplicator


def event(
    text: str,
    *,
    session_id: str = "session-1",
    stream_id: str = "microphone-1",
    source: Source = Source.MICROPHONE,
    is_final: bool = True,
) -> TranscriptEvent:
    return TranscriptEvent(
        session_id=session_id,
        segment_id="seg-1",
        stream_id=stream_id,
        source=source,
        start_ms=0,
        end_ms=1_000,
        text=text,
        is_final=is_final,
    )


class OverlapDeduplicatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dedup = OverlapDeduplicator()

    def emit(self, *texts: str, stream_id: str = "microphone-1") -> list[str]:
        return [
            self.dedup.deduplicate(event(text, stream_id=stream_id)).text
            for text in texts
        ]

    def test_english_overlap_is_removed_at_a_word_boundary(self) -> None:
        self.assertEqual(
            ["Let's review the proposal.", "Yes, I have it open now."],
            self.emit(
                "Let's review the proposal.",
                "the proposal. Yes, I have it open now.",
            ),
        )

    def test_chinese_overlap_is_removed_without_spaces_to_anchor_on(self) -> None:
        self.assertEqual(
            ["我们来看一下这个提案", "的第二部分"],
            self.emit("我们来看一下这个提案", "这个提案的第二部分"),
        )

    def test_code_switched_overlap_is_removed(self) -> None:
        self.assertEqual(
            ["我们用 WebSocket 连接", "已经建立"],
            self.emit("我们用 WebSocket 连接", "WebSocket 连接已经建立"),
        )

    def test_fully_repeated_chunk_collapses_to_empty_text(self) -> None:
        self.assertEqual(["hello there", ""], self.emit("hello there", "hello there"))

    def test_empty_result_does_not_poison_the_next_segment(self) -> None:
        # The service skips empty events, so the repeat must not extend the tail.
        self.assertEqual(
            ["hello there", "", "friends"],
            self.emit("hello there", "hello there", "there friends"),
        )

    def test_punctuation_only_differences_still_match(self) -> None:
        self.assertEqual(
            ["我们来看一下。", "然后开始"],
            self.emit("我们来看一下。", "看一下，然后开始"),
        )
        self.dedup.reset()
        self.assertEqual(
            ["Let's go", "right now"],
            self.emit("Let's go", "Lets go, right now"),
        )

    def test_matching_prefix_inside_a_longer_word_is_not_a_repeat(self) -> None:
        self.assertEqual(
            ["open now", "openness matters"],
            self.emit("open now", "openness matters"),
        )

    def test_short_latin_word_repeating_naturally_is_not_a_repeat(self) -> None:
        self.assertEqual(
            ["I will get back to", "To be honest, it works"],
            self.emit("I will get back to", "To be honest, it works"),
        )
        self.dedup.reset()
        self.assertEqual(
            ["That is so", "So we should ship it"],
            self.emit("That is so", "So we should ship it"),
        )

    def test_case_and_expanding_fold_keep_cut_positions_aligned(self) -> None:
        # "ß".casefold() is two characters; the cut must still land correctly.
        self.assertEqual(
            ["Auf der Straße", "geht es weiter"],
            self.emit("Auf der Straße", "Strasse geht es weiter"),
        )

    def test_streams_never_suppress_each_other(self) -> None:
        first = self.dedup.deduplicate(event("the proposal", stream_id="microphone-1"))
        second = self.dedup.deduplicate(
            event("the proposal", stream_id="system-1", source=Source.SYSTEM)
        )
        self.assertEqual("the proposal", first.text)
        self.assertEqual("the proposal", second.text)

    def test_partial_events_pass_through_untouched(self) -> None:
        self.dedup.deduplicate(event("the proposal"))
        partial = event("the proposal is ready", is_final=False)
        self.assertIs(partial, self.dedup.deduplicate(partial))
        # A partial must not become history either.
        self.assertEqual("is ready", self.dedup.deduplicate(event("proposal is ready")).text)

    def test_empty_text_passes_through_untouched(self) -> None:
        blank = event("")
        self.assertIs(blank, self.dedup.deduplicate(blank))

    def test_reset_forgets_one_stream_or_every_stream(self) -> None:
        self.emit("the proposal", stream_id="microphone-1")
        self.emit("the proposal", stream_id="system-1")
        self.dedup.reset("session-1", "microphone-1")
        self.assertEqual(["the proposal"], self.emit("the proposal", stream_id="microphone-1"))
        self.assertEqual([""], self.emit("the proposal", stream_id="system-1"))
        self.dedup.reset()
        self.assertEqual(["the proposal"], self.emit("the proposal", stream_id="system-1"))

    def test_unchanged_events_are_returned_by_identity(self) -> None:
        original = event("nothing came before this")
        self.assertIs(original, self.dedup.deduplicate(original))

    def test_deduplicated_event_preserves_every_other_field(self) -> None:
        self.dedup.deduplicate(event("the proposal"))
        result = self.dedup.deduplicate(event("the proposal is ready"))
        self.assertEqual("is ready", result.text)
        self.assertEqual("seg-1", result.segment_id)
        self.assertEqual("microphone-1", result.stream_id)
        self.assertEqual(Source.MICROPHONE, result.source)
        self.assertEqual(0, result.start_ms)
        self.assertEqual(1_000, result.end_ms)
        self.assertTrue(result.is_final)

    def test_history_is_bounded_by_the_configured_window(self) -> None:
        dedup = OverlapDeduplicator(max_overlap_chars=8)
        dedup.deduplicate(event("aaaaaaaaaaaa bbbb"))
        # Only the last eight characters remain available for matching.
        self.assertEqual(
            "aaaaaaaaaaaa cccc",
            dedup.deduplicate(event("aaaaaaaaaaaa cccc")).text,
        )

    def test_rejects_invalid_configuration(self) -> None:
        with self.assertRaises(ValueError):
            OverlapDeduplicator(max_overlap_chars=0)
        with self.assertRaises(ValueError):
            OverlapDeduplicator(min_match_chars=0)
        with self.assertRaises(ValueError):
            OverlapDeduplicator(min_latin_match_chars=0)


if __name__ == "__main__":
    unittest.main()


class ConcurrentSessionTests(unittest.TestCase):
    """Every client hardcodes the same stream ids, so history must be per session."""

    def test_sessions_sharing_a_stream_id_do_not_suppress_each_other(self) -> None:
        dedup = OverlapDeduplicator()
        first = event("Let's review the proposal.", session_id="session-a")
        second = event("the proposal is ready.", session_id="session-b")
        self.assertEqual("Let's review the proposal.", dedup.deduplicate(first).text)
        self.assertEqual("the proposal is ready.", dedup.deduplicate(second).text)

    def test_resetting_one_session_leaves_another_intact(self) -> None:
        dedup = OverlapDeduplicator()
        dedup.deduplicate(event("Let's review the proposal.", session_id="session-a"))
        dedup.deduplicate(event("Let's review the proposal.", session_id="session-b"))
        dedup.reset("session-a")
        self.assertEqual(
            "the proposal. Yes.",
            dedup.deduplicate(event("the proposal. Yes.", session_id="session-a")).text,
        )
        self.assertEqual(
            "Yes.",
            dedup.deduplicate(event("the proposal. Yes.", session_id="session-b")).text,
        )
