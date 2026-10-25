"""Source code must not depend on one developer's checkout location."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIRECTORIES = (
    "clients", "scripts", "services", "voxink_cli", "voxink_core", "voxink_dev",
    "voxink_mcp", "voxink_menubar", "voxink_web/src",
)
SUFFIXES = {".py", ".swift", ".ts", ".tsx", ".js", ".cmd", ".sh", ""}
SKIPPED = {".build", "node_modules", "DerivedData", "__pycache__"}
HOME_PATH = re.compile(r"(/Users/|/home/|C:\\\\Users\\\\)[A-Za-z0-9_.-]+")


class NoHomePathTests(unittest.TestCase):
    def test_source_files_hold_no_user_home_paths(self) -> None:
        offenders = []
        for directory in SOURCE_DIRECTORIES:
            for path in (ROOT / directory).rglob("*"):
                if not path.is_file() or path.suffix not in SUFFIXES or SKIPPED.intersection(path.parts):
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                offenders += [f"{path.relative_to(ROOT)}: {match.group(0)}" for match in HOME_PATH.finditer(text)]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
