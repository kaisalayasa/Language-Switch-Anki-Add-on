"""``ops/tts_runner.py``'s pure pieces -- the ``mw``/``aqt``-dependent orchestration in
``run_tts_batch`` itself needs a real Anki process and isn't exercised here, but this module
must stay *importable* without one (``aqt`` is a lazy, per-call import inside the function,
never at module scope) and ``default_concurrency`` is plain, testable logic.
"""

from __future__ import annotations

import unittest
from unittest import mock

from addon.ops.tts_runner import default_concurrency


class TestDefaultConcurrency(unittest.TestCase):
    def test_caps_at_four_on_a_big_machine(self):
        with mock.patch("os.cpu_count", return_value=32):
            self.assertEqual(default_concurrency(), 4)

    def test_scales_down_on_a_small_machine(self):
        with mock.patch("os.cpu_count", return_value=2):
            self.assertEqual(default_concurrency(), 2)

    def test_never_returns_less_than_one(self):
        with mock.patch("os.cpu_count", return_value=None):
            self.assertEqual(default_concurrency(), 1)
        with mock.patch("os.cpu_count", return_value=0):
            self.assertEqual(default_concurrency(), 1)


if __name__ == "__main__":
    unittest.main()
