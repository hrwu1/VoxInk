import os
import tempfile
import unittest
from pathlib import Path

from voxink_core.vocabulary import (
    MAX_CONTEXT_CHARS,
    MAX_FILE_BYTES,
    Vocabulary,
    VocabularyFile,
)


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
        # An editor reports the shortfall, so the two must agree.
        self.assertEqual(", ".join(vocabulary.biased_terms), context)
        self.assertLess(len(vocabulary.biased_terms), len(vocabulary.terms))

    def test_a_short_list_biases_every_term(self) -> None:
        vocabulary = Vocabulary.parse("VoxInk\nHrwu\n")

        self.assertEqual(vocabulary.biased_terms, vocabulary.terms)


class VocabularyEchoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vocabulary = Vocabulary.parse("吴昊苒\nGogo\n喷菇\n")

    def test_the_whole_list_read_back_is_an_echo(self) -> None:
        self.assertTrue(self.vocabulary.is_echo("吴昊苒，Gogo，喷菇。"))
        self.assertTrue(self.vocabulary.is_echo("吴昊苒， Gogo， 喷菇。"))
        self.assertTrue(self.vocabulary.is_echo("吴昊苒、gogo、喷菇"))

    def test_part_of_the_list_is_still_an_echo(self) -> None:
        self.assertTrue(self.vocabulary.is_echo("Gogo, 喷菇."))

    def test_one_term_alone_is_kept(self) -> None:
        self.assertFalse(self.vocabulary.is_echo("Gogo."))
        self.assertFalse(self.vocabulary.is_echo("Gogo, Gogo!"))

    def test_terms_inside_speech_are_kept(self) -> None:
        self.assertFalse(self.vocabulary.is_echo("吴昊苒，Gogo，喷菇来了。"))
        self.assertFalse(self.vocabulary.is_echo("I met Gogo and 喷菇 today."))

    def test_a_one_term_vocabulary_never_echoes(self) -> None:
        self.assertFalse(Vocabulary.parse("Gogo\n").is_echo("Gogo."))

    def test_an_empty_vocabulary_never_echoes(self) -> None:
        self.assertFalse(Vocabulary.parse("").is_echo("anything"))


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

    def test_text_returns_what_was_written_including_comments(self) -> None:
        self.path.write_text("# a note\n\nVoxInk\n", encoding="utf-8")

        self.assertEqual("# a note\n\nVoxInk\n", VocabularyFile(self.path).text())

    def test_text_of_a_missing_file_is_empty(self) -> None:
        self.assertEqual("", VocabularyFile(self.path).text())

    def test_writing_creates_the_file_and_updates_what_is_served(self) -> None:
        nested = Path(self.temp.name) / "new" / "vocabulary.txt"
        reader = VocabularyFile(nested)
        self.assertTrue(reader.current().is_empty)

        written = reader.write("Hrwu\n")

        self.assertEqual(("Hrwu",), written.terms)
        self.assertEqual(("Hrwu",), reader.current().terms)
        self.assertEqual("Hrwu\n", nested.read_text(encoding="utf-8"))

    def test_writing_more_than_the_cap_is_refused(self) -> None:
        reader = VocabularyFile(self.path)

        with self.assertRaises(ValueError):
            reader.write("x" * (MAX_FILE_BYTES + 1))

        self.assertFalse(self.path.exists())

    def test_writing_without_a_configured_path_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            VocabularyFile(None).write("Hrwu\n")

    def test_deleting_the_file_returns_to_an_empty_vocabulary(self) -> None:
        self.path.write_text("VoxInk\n", encoding="utf-8")
        reader = VocabularyFile(self.path)
        self.assertFalse(reader.current().is_empty)

        self.path.unlink()

        self.assertTrue(reader.current().is_empty)


if __name__ == "__main__":
    unittest.main()
