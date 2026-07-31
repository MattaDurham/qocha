"""The version is declared in two places (pyproject.toml and
qocha/__init__.py). They drifted two releases apart in July 2026 -
__version__ said 0.2.0 while v0.3.0 was tagged - so this pin exists to
make the release ritual's step 1 fail loudly when half-done."""
import re
import unittest
from pathlib import Path

import qocha


class VersionParity(unittest.TestCase):
    def test_dunder_version_matches_pyproject(self):
        text = (Path(__file__).resolve().parent.parent
                / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        self.assertIsNotNone(m, "pyproject.toml carries no version line")
        self.assertEqual(qocha.__version__, m.group(1))


if __name__ == "__main__":
    unittest.main()
