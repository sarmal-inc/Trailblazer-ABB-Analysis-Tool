"""Main view push to open windows. Run: python -m unittest tests.test_follow_view -v"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import multi_file_analyzer as m  # noqa: E402
from test_binned_view import BinBase  # noqa: E402


class FakeWindow:
    def __init__(self):
        self._closed = False
        self.calls = []

    def follow_view(self, lo, hi):
        self.calls.append((lo, hi))

    def close(self):
        self._closed = True


class FollowPushTest(BinBase):
    def setUp(self):
        super().setUp()
        self.fake = FakeWindow()
        self.app._histogram = self.fake

    def cleanup(self):
        self.app._histogram = None
        super().cleanup()

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.update()
            time.sleep(0.01)

    def settle(self):
        self.pump(0.6)
        self.fake.calls.clear()

    def test_delay_constant(self):
        self.assertEqual(m.FOLLOW_DELAY_MS, 400)

    def test_zoom_pushes_once_after_delay(self):
        self.settle()
        self.app.tools.zoom_x(1, 2)
        self.app.update()
        self.app.tools.zoom_x(1.5, 2.5)
        self.pump(0.6)
        self.assertEqual(len(self.fake.calls), 1, self.fake.calls)
        lo, hi = self.fake.calls[0]
        self.assertAlmostEqual(lo, 1.5, 9)
        self.assertAlmostEqual(hi, 2.5, 9)

    def test_no_push_before_delay(self):
        self.settle()
        self.app.tools.zoom_x(1, 2)
        self.pump(0.2)
        self.assertEqual(self.fake.calls, [])

    def test_reset_pushes_full_view(self):
        self.settle()
        self.app.tools.zoom_x(1, 2)
        self.pump(0.6)
        self.app.tools.reset()
        self.pump(0.6)
        self.assertTrue(self.fake.calls)
        lo, hi = self.fake.calls[-1]
        x0, x1 = self.app.ax.get_xlim()
        self.assertAlmostEqual(lo, x0, 9)
        self.assertAlmostEqual(hi, x1, 9)
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 3.5)

    def test_main_view_range(self):
        r = self.app.main_view_range()
        self.assertIsNotNone(r)
        self.app.tools.zoom_x(1, 2)
        self.assertEqual(tuple(round(x, 9) for x in self.app.main_view_range()), (1.0, 2.0))

    def test_no_runs_no_push(self):
        self.settle()
        for f in self.app.files:
            f["show"] = False
        self.app.draw()
        self.pump(0.6)
        self.assertEqual(self.fake.calls, [])
        self.assertIsNone(self.app.main_view_range())

    def test_closed_window_not_pushed(self):
        self.settle()
        self.app.tools.zoom_x(1, 2)
        self.fake._closed = True
        self.pump(0.6)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
