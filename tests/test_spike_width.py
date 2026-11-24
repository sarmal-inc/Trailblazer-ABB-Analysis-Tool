"""FWHM of spikes with the overlapped rule.
Run: python -m unittest tests.test_spike_width -v"""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import spike_width  # noqa: E402
from trace_store import TraceData  # noqa: E402

DT = 2e-6


def _trace(v, dt=DT, t=None):
    v = np.asarray(v, dtype=np.float64)
    if t is None:
        t = np.round(np.arange(len(v)) * dt, 7)
    return TraceData.from_arrays(t, np.round(v, 4))


def _flat(n=400, floor=0.0):
    return np.full(n, floor)


def _fwhm(tr, peaks, baseline=0.0, i0=0, i1=None, **kw):
    return spike_width.fwhm(tr, np.asarray(peaks, dtype=np.int64), baseline, i0,
                            tr.n if i1 is None else i1, **kw)


class FwhmTests(unittest.TestCase):
    def test_triangle_pulse_exact(self):
        v = _flat(); v[98:103] = [0, 50, 100, 50, 0]
        np.testing.assert_allclose(_fwhm(_trace(v), [100]), [2 * DT], rtol=1e-6)

    def test_interpolated_crossing(self):
        v = _flat(); v[98:103] = [0, 40, 100, 40, 0]
        np.testing.assert_allclose(_fwhm(_trace(v), [100]), [2 * DT * (1 - 1 / 6)], rtol=1e-6)

    def test_baseline_shifts_half_max(self):
        v = _flat(floor=20.0); v[98:103] = [20, 60, 120, 60, 20]
        np.testing.assert_allclose(_fwhm(_trace(v), [100], baseline=20.0), [2 * DT * (1 - 1 / 6)], rtol=1e-6)

    def test_overlapped_neighbour_stays_above_half(self):
        v = _flat(); v[100] = 100; v[101:130] = 70; v[130] = 100
        self.assertTrue(np.isnan(_fwhm(_trace(v), [100, 130])).all())

    def test_neighbour_with_dip_below_half_is_clean(self):
        v = _flat(); v[100] = 100; v[101:130] = 70; v[115] = 10; v[130] = 100
        res = _fwhm(_trace(v), [100, 130])
        self.assertTrue(np.isfinite(res).all(), res)

    def test_walk_limit(self):
        v = _flat(); v[100] = 100; v[101:251] = 60      # 300 us tail above half-max
        self.assertTrue(np.isnan(_fwhm(_trace(v), [100]))[0])
        v[101:151] = 60; v[151:251] = 0                 # 100 us tail: inside the 200 us limit
        self.assertTrue(np.isfinite(_fwhm(_trace(v), [100]))[0])

    def test_window_edge(self):
        v = _flat(); v[50:53] = [100, 40, 0]; v[300:303] = [0, 40, 100]
        tr = _trace(v)
        self.assertTrue(np.isnan(_fwhm(tr, [50], i0=50))[0])
        self.assertTrue(np.isnan(_fwhm(tr, [302], i1=303))[0])

    def test_10khz_min_walk_samples(self):
        v = _flat(); v[99:105] = [0, 100, 60, 60, 60, 0]       # right crossing 4 samples away
        res = _fwhm(_trace(v, dt=1e-4), [100])
        self.assertTrue(np.isfinite(res[0]), res)

    def test_non_uniform_time_interpolates_on_real_times(self):
        n = 400
        t = np.round(np.cumsum(np.where(np.arange(n) % 2, 2e-6, 3e-6)), 6)
        v = _flat(n); v[98:103] = [0, 50, 100, 50, 0]
        tr = _trace(v, t=t)
        self.assertIsNone(tr.dt)
        np.testing.assert_allclose(_fwhm(tr, [100]), [t[101] - t[99]], rtol=1e-6)

    def test_chunking_matches_single_pass(self):
        rng = np.random.default_rng(1)
        v = rng.normal(0, 1, 20000)
        peaks = np.sort(rng.choice(np.arange(20, 19980, 40), 50, replace=False))
        for p in peaks:
            v[p - 2:p + 3] = [5, 40, 100, 50, 10]
        tr = _trace(v)
        a, b = _fwhm(tr, peaks, chunk=7), _fwhm(tr, peaks, chunk=20_000)
        np.testing.assert_array_equal(np.isnan(a), np.isnan(b))
        np.testing.assert_allclose(a[np.isfinite(a)], b[np.isfinite(b)])
        self.assertTrue(np.isfinite(a).all())

    def test_staged_walk_matches_full_window(self):
        rng = np.random.default_rng(2)
        v = rng.normal(0, 1, 60000)
        peaks = np.arange(200, 59800, 300)
        tails = rng.integers(1, 160, len(peaks))        # 2 us .. 320 us above half-max on the right
        for p, k in zip(peaks, tails):
            v[p - 2:p + 1] = [5, 40, 100]
            v[p + 1:p + 1 + k] = 60
        tr = _trace(v)
        staged = _fwhm(tr, peaks)
        with mock.patch.object(spike_width, "FIRST_STAGES", ()):
            full = _fwhm(tr, peaks)
        np.testing.assert_array_equal(np.isnan(staged), np.isnan(full))
        np.testing.assert_allclose(staged[np.isfinite(staged)], full[np.isfinite(full)])
        self.assertTrue(np.isnan(full).any() and np.isfinite(full).any())

    def test_empty(self):
        self.assertEqual(len(_fwhm(_trace(_flat()), [])), 0)


if __name__ == "__main__":
    unittest.main()
