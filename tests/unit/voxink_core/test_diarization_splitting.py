from __future__ import annotations

import unittest

from voxink_core.models import Source, WordTimestamp
from voxink_core.diarization.models import SpeakerTurn
from voxink_core.diarization.splitting import (
    UNALIGNED,
    SplitPolicy,
    map_words,
    split_block,
)
from voxink_core.diarization.transcript import TranscriptBlock, block_digest


def block(text: str, start_ms: int = 0, end_ms: int = 15_000) -> TranscriptBlock:
    return TranscriptBlock(
        part="transcript-part-001.md",
        block=1,
        source=Source.MICROPHONE,
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
        speaker=None,
        sha256=block_digest(text),
        header=f"**[{start_ms}-{end_ms}] [MIC]**",
    )


def turn(speaker: str, start_ms: int, end_ms: int) -> SpeakerTurn:
    return SpeakerTurn(speaker, start_ms, end_ms, Source.MICROPHONE, True)


def words(*items: tuple[str, int, int]) -> tuple[WordTimestamp, ...]:
    return tuple(WordTimestamp(text, start, end) for text, start, end in items)


def spaced(text: str, start_ms: int, step_ms: int) -> tuple[WordTimestamp, ...]:
    """One CJK character per `step_ms`, the way the aligner tokenizes Mandarin."""
    return tuple(
        WordTimestamp(character, start_ms + index * step_ms, start_ms + (index + 1) * step_ms)
        for index, character in enumerate(text)
    )


def texts(block_, pieces):
    return [block_.text[piece.text_start : piece.text_end] for piece in pieces]


class MapWordsTests(unittest.TestCase):
    def test_cjk_characters_map_one_to_one_past_punctuation(self):
        spans = map_words("你好，世界。", words(("你", 0, 1), ("好", 1, 2), ("世", 3, 4), ("界", 4, 5)))
        self.assertEqual([(s.text_start, s.text_end) for s in spans], [(0, 1), (1, 2), (3, 4), (4, 5)])

    def test_latin_words_are_matched_after_the_aligner_stripped_punctuation(self):
        spans = map_words(
            "Well, it's done (finally).",
            words(("Well", 0, 1), ("it's", 1, 2), ("done", 2, 3), ("finally", 3, 4)),
        )
        self.assertEqual(
            [(s.text_start, s.text_end) for s in spans], [(0, 4), (6, 10), (11, 15), (17, 24)]
        )

    def test_skipping_a_letter_means_the_words_are_from_other_text(self):
        self.assertIsNone(map_words("one two three", words(("one", 0, 1), ("three", 1, 2))))

    def test_a_word_not_in_the_text_fails_the_whole_block(self):
        self.assertIsNone(map_words("你好", words(("你", 0, 1), ("坏", 1, 2))))

    def test_mixed_script_maps_characters_and_words(self):
        spans = map_words(
            "因为喷菇非常considerate，他会。",
            words(("因", 0, 1), ("为", 1, 2), ("喷", 2, 3), ("菇", 3, 4), ("非", 4, 5), ("常", 5, 6),
                  ("considerate", 6, 7), ("他", 7, 8), ("会", 8, 9)),
        )
        self.assertEqual((spans[6].text_start, spans[6].text_end), (6, 17))
        self.assertEqual((spans[-1].text_start, spans[-1].text_end), (19, 20))


class SplitTests(unittest.TestCase):
    def test_a_two_speaker_block_is_cut_where_the_speaker_changes(self):
        text = "可能太香了。真的好吃。我要生气了。"
        block_ = block(text)
        aligned = spaced(text.replace("。", ""), 100, 200)
        pieces = split_block(
            block_,
            aligned,
            [turn("MIC_SPEAKER_01", 0, 1_100), turn("MIC_SPEAKER_00", 1_100, 4_000)],
        )
        self.assertEqual([p.speaker_id for p in pieces], ["MIC_SPEAKER_01", "MIC_SPEAKER_00"])
        self.assertEqual(texts(block_, pieces), ["可能太香了。", "真的好吃。我要生气了。"])
        self.assertEqual("".join(texts(block_, pieces)), text)
        self.assertEqual(pieces[0].start_ms, 100)
        self.assertEqual(pieces[1].end_ms, 100 + 14 * 200)
        self.assertTrue(all(p.speaker_coverage == 1.0 for p in pieces))

    def test_one_speaker_throughout_yields_one_piece(self):
        text = "一句话。"
        pieces = split_block(block(text), spaced("一句话", 0, 100), [turn("MIC_SPEAKER_00", 0, 500)])
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].speaker_id, "MIC_SPEAKER_00")
        self.assertEqual((pieces[0].text_start, pieces[0].text_end), (0, len(text)))

    def test_a_word_in_a_gap_adopts_the_nearest_turn_within_the_gap_limit(self):
        pieces = split_block(
            block("好吃"),
            words(("好", 0, 100), ("吃", 500, 600)),
            [turn("MIC_SPEAKER_00", 0, 100)],
            SplitPolicy(max_gap_ms=750),
        )
        self.assertEqual([p.speaker_id for p in pieces], ["MIC_SPEAKER_00"])

    def test_a_word_too_far_from_any_turn_follows_the_word_before_it(self):
        pieces = split_block(
            block("好吃"),
            words(("好", 0, 100), ("吃", 5_000, 5_100)),
            [turn("MIC_SPEAKER_00", 0, 100)],
            SplitPolicy(max_gap_ms=750),
        )
        self.assertEqual([p.speaker_id for p in pieces], ["MIC_SPEAKER_00"])
        self.assertEqual(pieces[0].attributed_ms, 100)

    def test_quantized_instants_follow_the_preceding_word(self):
        """Neighbouring characters often share one aligner instant."""
        pieces = split_block(
            block("真的没有你，好。"),
            words(("真", 0, 100), ("的", 100, 100), ("没", 100, 100), ("有", 100, 100), ("你", 100, 200),
                  ("好", 2_000, 2_100)),
            [turn("MIC_SPEAKER_00", 0, 200), turn("MIC_SPEAKER_01", 2_000, 2_100)],
        )
        self.assertEqual([p.speaker_id for p in pieces], ["MIC_SPEAKER_00", "MIC_SPEAKER_01"])
        self.assertEqual(texts(block("真的没有你，好。"), pieces), ["真的没有你，", "好。"])

    def test_an_instant_inside_a_turn_is_that_turns_word(self):
        pieces = split_block(
            block("啊嗯"),
            words(("啊", 0, 100), ("嗯", 900, 900)),
            [turn("MIC_SPEAKER_00", 0, 100), turn("MIC_SPEAKER_01", 800, 1_200)],
            SplitPolicy(min_island_ms=0),
        )
        self.assertEqual([p.speaker_id for p in pieces], ["MIC_SPEAKER_00", "MIC_SPEAKER_01"])

    def test_words_stacked_on_one_instant_stay_unlabeled(self):
        """A hallucinated tail the audio does not hold gets no speaker."""
        text = "真的好吃。你是说你想再睡会儿。"
        placed = spaced("真的好吃", 0, 200)
        stacked = tuple(WordTimestamp(c, 14_720, 14_720) for c in "你是说你想再睡会儿")
        pieces = split_block(block(text), placed + stacked, [turn("MIC_SPEAKER_00", 0, 15_000)])
        self.assertEqual([p.speaker_id for p in pieces], ["MIC_SPEAKER_00", None])
        self.assertEqual(pieces[1].reason, UNALIGNED)
        self.assertEqual(texts(block(text), pieces), ["真的好吃。", "你是说你想再睡会儿。"])

    def test_words_timed_past_the_block_end_stay_unlabeled(self):
        text = "好吃。没事儿。"
        aligned = spaced("好吃", 0, 200) + spaced("没事儿", 15_600, 100)
        pieces = split_block(block(text, 0, 15_000), aligned, [turn("MIC_SPEAKER_00", 0, 16_000)])
        self.assertEqual([p.speaker_id for p in pieces], ["MIC_SPEAKER_00", None])

    def test_a_short_island_between_agreeing_runs_is_absorbed(self):
        text = "我们今天去公园"
        aligned = spaced(text, 0, 200)
        pieces = split_block(
            block(text),
            aligned,
            [
                turn("MIC_SPEAKER_00", 0, 600),
                turn("MIC_SPEAKER_01", 600, 800),
                turn("MIC_SPEAKER_00", 800, 1_400),
            ],
            SplitPolicy(min_island_ms=300),
        )
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].speaker_id, "MIC_SPEAKER_00")
        self.assertAlmostEqual(pieces[0].speaker_coverage, 1_200 / 1_400)

    def test_a_long_interjection_survives(self):
        text = "我们今天去公园"
        aligned = spaced(text, 0, 200)
        pieces = split_block(
            block(text),
            aligned,
            [
                turn("MIC_SPEAKER_00", 0, 600),
                turn("MIC_SPEAKER_01", 600, 1_000),
                turn("MIC_SPEAKER_00", 1_000, 1_400),
            ],
            SplitPolicy(min_island_ms=300),
        )
        self.assertEqual(
            [p.speaker_id for p in pieces], ["MIC_SPEAKER_00", "MIC_SPEAKER_01", "MIC_SPEAKER_00"]
        )

    def test_words_that_do_not_fit_the_text_mean_no_split(self):
        self.assertEqual(
            split_block(block("完全不同"), spaced("另一段", 0, 100), [turn("MIC_SPEAKER_00", 0, 500)]),
            (),
        )

    def test_no_placed_word_means_no_split(self):
        self.assertEqual(
            split_block(block("好吃"), spaced("好吃", 0, 100), [turn("MIC_SPEAKER_00", 9_000, 9_500)]),
            (),
        )

    def test_pieces_tile_the_text_including_leading_punctuation(self):
        text = "（笑）好的。不。"
        aligned = words(("笑", 0, 100), ("好", 200, 300), ("的", 300, 400), ("不", 2_000, 2_100))
        pieces = split_block(
            block(text), aligned, [turn("MIC_SPEAKER_00", 0, 400), turn("MIC_SPEAKER_01", 2_000, 2_100)]
        )
        self.assertEqual(pieces[0].text_start, 0)
        self.assertEqual(pieces[-1].text_end, len(text))
        self.assertEqual("".join(texts(block(text), pieces)), text)

    def test_only_exclusive_turns_count(self):
        raw = SpeakerTurn("MIC_SPEAKER_01", 0, 500, Source.MICROPHONE, False)
        pieces = split_block(block("好吃"), spaced("好吃", 0, 100), [raw, turn("MIC_SPEAKER_00", 0, 500)])
        self.assertEqual([p.speaker_id for p in pieces], ["MIC_SPEAKER_00"])

    def test_policy_rejects_nonsense(self):
        with self.assertRaises(ValueError):
            SplitPolicy(collapse_run_words=1)
        with self.assertRaises(ValueError):
            SplitPolicy(max_gap_ms=-1)


class SnapTests(unittest.TestCase):
    def test_a_cut_one_character_past_punctuation_moves_back_to_it(self):
        text = "怎么了？不是，我在想。"
        aligned = spaced("怎么了不是我在想", 0, 200)
        # The aligner put 不 (index 3) in the first speaker's time.
        pieces = split_block(
            block(text),
            aligned,
            [turn("MIC_SPEAKER_01", 0, 800), turn("MIC_SPEAKER_00", 800, 1_600)],
            SplitPolicy(min_island_ms=0),
        )
        self.assertEqual(texts(block(text), pieces), ["怎么了？", "不是，我在想。"])
        self.assertEqual(pieces[0].end_ms, 600)
        self.assertEqual(pieces[1].start_ms, 600)

    def test_a_cut_one_character_before_punctuation_moves_forward(self):
        text = "很美味的，很好吃。"
        aligned = spaced("很美味的很好吃", 0, 200)
        pieces = split_block(
            block(text),
            aligned,
            [turn("MIC_SPEAKER_00", 0, 1_000), turn("MIC_SPEAKER_01", 1_000, 1_400)],
            SplitPolicy(min_island_ms=0),
        )
        self.assertEqual(texts(block(text), pieces), ["很美味的，", "很好吃。"])

    def test_a_cut_already_at_punctuation_stays(self):
        text = "太香了。好吃。"
        pieces = split_block(
            block(text),
            spaced("太香了好吃", 0, 200),
            [turn("MIC_SPEAKER_01", 0, 600), turn("MIC_SPEAKER_00", 600, 1_000)],
        )
        self.assertEqual(texts(block(text), pieces), ["太香了。", "好吃。"])

    def test_no_boundary_within_reach_leaves_the_cut_alone(self):
        text = "一二三四五六七八"
        pieces = split_block(
            block(text),
            spaced(text, 0, 200),
            [turn("MIC_SPEAKER_01", 0, 800), turn("MIC_SPEAKER_00", 800, 1_600)],
        )
        self.assertEqual(texts(block(text), pieces), ["一二三四", "五六七八"])

    def test_snapping_never_empties_a_run(self):
        text = "不，是。"
        pieces = split_block(
            block(text),
            spaced("不是", 0, 200),
            [turn("MIC_SPEAKER_01", 0, 200), turn("MIC_SPEAKER_00", 200, 400)],
            SplitPolicy(min_island_ms=0),
        )
        self.assertEqual(texts(block(text), pieces), ["不，", "是。"])

    def test_english_words_cut_at_a_space_are_left_alone(self):
        text = "we should go now"
        aligned = words(("we", 0, 100), ("should", 100, 200), ("go", 200, 300), ("now", 300, 400))
        pieces = split_block(
            block(text),
            aligned,
            [turn("MIC_SPEAKER_00", 0, 200), turn("MIC_SPEAKER_01", 200, 400)],
            SplitPolicy(min_island_ms=0),
        )
        self.assertEqual(texts(block(text), pieces), ["we should ", "go now"])


if __name__ == "__main__":
    unittest.main()
