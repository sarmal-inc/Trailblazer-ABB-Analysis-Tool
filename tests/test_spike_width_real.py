"""Spike width on real runs: timing and sanity (skipped unless the files are local, not cloud-only).
Test 008 run_001: 500 kHz, 7.4 M samples, noise (no light). Test 015 run_007: 10 kHz luciferase
positive with dense pulses.
Run: python -m unittest tests.test_spike_width_real -v"""
import gc
import os
import shutil
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import histogram  # noqa: E402
import multi_file_analyzer as m  # noqa: E402
import noise  # noqa: E402
import spike_width  # noqa: E402
import trace_store as ts  # noqa: E402

BB = (r"C:\Users\MannaDoud\Sarmal\Project Trailblazer - Engineering\Bread Board"
      r"\Bread Board Experiments and Test")
RUN_500K = os.path.join(BB, r"Test 008_ABB Noise Identification\620nm Config"
                            r"\261106 Noise Before Grounding\csv files\run_001.csv")
RUN_POS = os.path.join(BB, r"Test 015 LLC in solution\261117 Second test\Luciferace 2026-11-17\run_007.csv")
MAX_SHARE = 0.25     # 500 kHz: FWHM time / (baseline + spike detection time) of the row
MAX_SECONDS = 0.1    # any run: FWHM time per row


def _local(p):
    return os.path.exists(p) and not ts.is_cloud_only(p)


def _best(fn, n=3):
    best, out = float("inf"), None
    for _ in range(n):
        t = time.perf_counter()
        out = fn()
        best = min(best, time.perf_counter() - t)
    return best, out


class _Real:
    PATH = None
    K = 10.0               # threshold = robust baseline + K sigma
    CHECK_SHARE = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bbcache_width_")
        cls.tr = ts.open_traces(cls.PATH, m.parse_file, cls.tmp)[0][0]["data"]

    @classmethod
    def tearDownClass(cls):
        cls.tr = None
        gc.collect()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_width_overhead(self):
        tr = self.tr
        i0, i1 = 0, tr.n
        t_base, (base, sigma) = _best(lambda: histogram.robust_baseline(*histogram.level_counts(tr, i0, i1)))
        thr = base + self.K * sigma
        t_sp, sp = _best(lambda: noise.find_spikes_at(tr, tr.t_start, tr.t_end, thr))
        t_fw, fw = _best(lambda: spike_width.fwhm(tr, sp.peak_idx, base, i0, i1))
        share = t_fw / (t_base + t_sp)
        n = len(sp.times)
        above = histogram.width_stats(sp.width_above * 1e6)
        full = histogram.width_stats(fw * 1e6)
        print(f"\n{os.path.basename(os.path.dirname(self.PATH))}/{os.path.basename(self.PATH)}: "
              f"dt={tr.dt * 1e6 if tr.dt else None} us, n={tr.n:,}, thr={thr:.3f} mV, spikes={n:,}, "
              f"baseline {t_base:.3f} s, spikes {t_sp:.3f} s, fwhm {t_fw:.3f} s ({share:.0%}); "
              f"overlapped {int(np.isnan(fw).sum()):,}; median above {above[0]} us, median FWHM {full[0]} us")
        self.assertEqual(len(fw), n)
        self.assertGreater(n, 1000)
        self.assertLessEqual(t_fw, MAX_SECONDS)
        if self.CHECK_SHARE:
            self.assertLessEqual(share, MAX_SHARE)


@unittest.skipUnless(_local(RUN_500K), "Test 008 run_001 not available locally")
class Real500kTest(_Real, unittest.TestCase):
    PATH = RUN_500K
    K = 2.5                # a noise run: a low threshold so there are spikes to measure
    CHECK_SHARE = True


@unittest.skipUnless(_local(RUN_POS), "Test 015 run_007 not available locally")
class RealPositiveTest(_Real, unittest.TestCase):
    PATH = RUN_POS


if __name__ == "__main__":
    unittest.main()
