import os
import tempfile
import unittest
from pathlib import Path

from voxink_core.vocabulary import MAX_CONTEXT_CHARS, Vocabulary, VocabularyFile


class VocabularyParsingTests(unittest.TestCase):
    def test_reads_terms_rules_and_ignores_comments(self) -> None:
        vocabulary = Vocabulary.parse(
            "\n".join(
                (
                    "# names",
                    "",
                    "VoxInk",
                    "  ScreenCaptureKit  ",
                    "hallow => Hrwu",
                    "   ",
                )
            )
        )

        self.assertEqual(vocabulary.terms, ("VoxInk", "ScreenCaptureKit", "Hrwu"))
        self.assertEqual(vocabulary.corrections, (("hallow", "Hrwu"),))
        self.assertEqual(vocabulary.context, "VoxInk, ScreenCaptureKit, Hrwu")

    def test_a_rule_does_not_repeat_a_term_already_listed(self) -> None:
        vocabulary = Vocabulary.parse("Hrwu\nhallow => Hrwu\n")

        self.assertEqual(vocabulary.terms, ("Hrwu",))

    def test_an_incomplete_rule_is_skipped_without_losing_the_rest(self) -> None:
        vocabulary = Vocabulary.parse("=> Hrwu\nhallow =>\nVoxInk\n")

        self.assertEqual(vocabulary.corrections, ())
        self.assertEqual(vocabulary.terms, ("VoxInk",))

    def test_an_empty_file_biases_nothing(self) -> None:
        vocabulary = Vocabulary.parse("# only a comment\n")

        self.assertTrue(vocabulary.is_empty)
        self.assertIsNone(vocabulary.context)

    def test_context_is_truncated_on_a_term_boundary(self) -> None:
        vocabulary = Vocabulary.parse("\n".join(["term" + str(index) for index in range(600)]))

        context = vocabulary.context

        self.assertIsNotNone(context)
        assert context is not None
        self.assertLessEqual(len(context), MAX_CONTEXT_CHARS)
        self.assertFalse(context.endswith(","))
        self.assertIn("term0", context)


class VocabularyCorrectionTests(unittest.TestCase):
    def test_replaces_a_whole_word_case_insensitively(self) -> None:
        vocabulary = Vocabulary.parse("hallow => Hrwu")

        self.assertEqual(vocabulary.apply("I met Hallow today."), "I met Hrwu today.")

    def test_never_rewrites_the_middle_of_a_longer_word(self) -> None:
        vocabulary = Vocabulary.parse("hallow => Hrwu")

        self.assertEqual(vocabulary.apply("Halloween is hallowed."), "Halloween is hallowed.")

    def test_rules_apply_in_file_order(self) -> None:
        vocabulary = Vocabulary.parse("vox ink => VoxInk\nVoxInk => VoxInk app")

        self.assertEqual(vocabulary.apply("open vox ink"), "open VoxInk app")

    def test_a_replacement_containing_a_backslash_is_inserted_literally(self) -> None:
        vocabulary = Vocabulary.parse(r"slash => a\1b")

        self.assertEqual(vocabulary.apply("a slash here"), r"a a\1b here")

    def test_corrects_text_without_spaces_around_the_term(self) -> None:
        vocabulary = Vocabulary.parse("你好 => 妳好")

        self.assertEqual(vocabulary.apply("他说你好啊"), "他说妳好啊")

    def test_text_is_untouched_when_no_rule_matches(self) -> None:
        vocabulary = Vocabulary.parse("hallow => Hrwu")

        self.assertEqual(vocabulary.apply("nothing to fix"), "nothing to fix")


class VocabularyFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "vocabulary.txt"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_a_missing_file_is_an_empty_vocabulary(self) -> None:
        self.assertTrue(VocabularyFile(self.path).current().is_empty)

    def test_no_configured_path_is_an_empty_vocabulary(self) -> None:
        self.assertTrue(VocabularyFile(None).current().is_empty)

    def test_an_edit_is_picked_up_without_recreating_the_reader(self) -> None:
        self.path.write_text("VoxInk\n", encoding="utf-8")
        reader = VocabularyFile(self.path)
        self.assertEqual(reader.current().terms, ("VoxInk",))

        self.path.write_text("VoxInk\nHrwu\n", encoding="utf-8")
        # A same-second rewrite must still be seen, so the check cannot rely on
        # whole-second modification times.
        os.utime(self.path, ns=(0, 0))

        self.assertEqual(reader.current().terms, ("VoxInk", "Hrwu"))

    def test_deleting_the_file_returns_to_an_empty_vocabulary(self) -> None:
        self.path.write_text("VoxInk\n", encoding="utf-8")
        reader = VocabularyFile(self.path)
        self.assertFalse(reader.current().is_empty)

        self.path.unlink()

        self.assertTrue(reader.current().is_empty)


if __name__ == "__main__":
    unittest.main()
