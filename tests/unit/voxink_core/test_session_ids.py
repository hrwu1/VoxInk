from datetime import datetime, timedelta, timezone
import re
import unittest

from voxink_core.session_ids import new_session_id


class SessionIDTests(unittest.TestCase):
    def test_formats_an_aware_timestamp_with_six_fractional_digits(self) -> None:
        eastern = timezone(timedelta(hours=-4))
        instant = datetime(2026, 8, 31, 6, 31, 3, 931_042, tzinfo=eastern)

        self.assertEqual("2026-08-31_06-31-03-931042", new_session_id(instant))

    def test_default_uses_the_canonical_shape(self) -> None:
        self.assertRegex(
            new_session_id(),
            re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{6}$"),
        )

    def test_rejects_a_timestamp_without_a_timezone(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            new_session_id(datetime(2026, 8, 31, 6, 31, 3))


if __name__ == "__main__":
    unittest.main()
