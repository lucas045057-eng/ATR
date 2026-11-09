"""Namespace browser fixtures while preserving existing flat test-helper imports."""

import sys
from pathlib import Path

_BROWSER_TESTS = str(Path(__file__).resolve().parent)
if _BROWSER_TESTS not in sys.path:
    sys.path.insert(0, _BROWSER_TESTS)
