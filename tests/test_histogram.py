"""histogram.py: DAQ-level bins, densities, reference level, per-gain comparison.
Run: python -m unittest tests.test_histogram -v"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import histogram as h  # noqa: E402
from trace_store import TraceData  # noqa: E402

LSB = 0.152588


def grid_levels(lo=-30, hi=30, skip=()):
    return np.array([round(k * LSB, 4) for k in range(lo, hi + 1) if k not in skip])


class LevelTests(unittest.TestCase):
    def test_level_counts_exact(self):
        levels = grid_levels(skip=(3,))
        rng = np.random.default_rng(0)
        v = levels[rng.integers(0, len(levels), 5000)]
        tr = TraceData.from_arrays(np.round(np.arange(5000) * 1e-4, 4), v)
        lv, ct = h.level_counts(tr, 100, 4100)
        self.assertNotIn(round(3 * LSB, 4), set(np.round(lv, 4)))
        self.assertEqual(int(ct.sum()), 4000)
        self.assertTrue(np.all(np.diff(lv) > 0))
        np.testing.assert_array_equal(np.round(lv, 4), np.unique(v[100:4100]))

    def test_daq_step(self):
        self.assertAlmostEqual(h.daq_step(grid_levels()), LSB, delta=2e-5)
        self.assertEqual(h.daq_step(np.array([1.0])), 1.0)

    def test_skipped_level_is_empty_bin(self):
        levels = grid_levels(skip=(3,))
        counts = np.full(len(levels), 10)
        edges = h.sample_edges([levels], 0.0, levels.max(), levels.min())
        d = h.sample_density(levels, counts, edges, 0.0, 1.0)
        i = np.searchsorted(edges, 3 * LSB) - 1
        self.assertEqual(d[i], 0)
        self.assertGreater(d[i - 1], 0)
        self.assertGreater(d[i + 1], 0)

    def test_density_scaling(self):
        levels = grid_levels()
        counts = np.zeros(len(levels)); counts[30] = 1000                 # all at 0 mV
        edges = h.sample_edges([levels], 0.0, levels.max(), levels.min())
        d = h.sample_density(levels, counts, edges, 0.0, 2.0)
        i = np.searchsorted(edges, 0.0) - 1
        self.assertAlmostEqual(d[i], 1000 / 2.0 / (edges[i + 1] - edges[i]))
        self.assertAlmostEqual(d.sum() * 0 + (d * np.diff(edges)).sum(), 500.0)

    def test_edges_with_log_tails(self):
        levels = np.concatenate([grid_levels(), [300.0, -40.0]])
        e = h.sample_edges([levels], 0.0, 300.0, -40.0)
        self.assertTrue(np.all(np.diff(e) > 0))
        self.assertGreaterEqual(e[-1], 300.0)
        self.assertLessEqual(e[0], -40.0)
        lin = e[(e > -h.LIN_MV) & (e < h.LIN_MV)]
        self.assertAlmostEqual(np.median(np.diff(lin)), LSB, delta=2e-5)

    def test_edges_mixed_steps(self):
        a = grid_levels()
        b = np.round(np.arange(-500, 501) * 0.0001, 4)
        e = h.sample_edges([a, b, np.array([300.0])], 0.2, 300.0, a.min())
        self.assertTrue(np.all(np.diff(e) > 0))
        lin = e[(e > -h.LIN_MV) & (e < h.LIN_MV)]
        self.assertLessEqual(len(lin), 20_001 + 2)

    def test_spike_edges(self):
        e = h.spike_edges(5.0, 5000.0)
        self.assertAlmostEqual(e[0], 5.0)
        self.assertAlmostEqual(e[-1], 5000.0)
        self.assertTrue(89 <= len(e) - 1 <= 91)
        self.assertIsNone(h.spike_edges(0.0, 100.0))
        self.assertEqual(len(h.spike_edges(5.0, 5.0)), 2)

    def test_robust_baseline_ignores_many_pulses(self):
        rng = np.random.default_rng(1)
        v = np.round(rng.normal(0.2, 0.45, 100_000), 4)
        v[rng.choice(100_000, 17_000, replace=False)] = rng.uniform(20, 2000, 17_000)   # 17 % in pulses
        lv, ct = np.unique(v, return_counts=True)
        base, sd = h.robust_baseline(lv, ct)
        self.assertAlmostEqual(base, 0.2, delta=0.15)
        self.assertAlmostEqual(sd, 0.45, delta=0.2)
        self.assertEqual(h.robust_baseline(np.array([1.0]), np.array([5])), (1.0, 0.0))

    def test_reference_level(self):
        self.assertEqual(h.reference_level([1.0, 2.0, 10.0], ["negative", "negative", "positive"]), 1.5)
        self.assertEqual(h.reference_level([1.0, 2.0, 10.0], ["positive", "none", "positive"]), 2.0)


def rs(i, rate_n, gain=1200.0, role="positive", run=None, t_from=0.0, t_to=10.0, duration=10.0):
    return h.RowStat(i, run if run is not None else i, gain, role, t_from, t_to, rate_n, duration)


class CompareTests(unittest.TestCase):
    def test_compare_matches_hand_values(self):
        rows = [rs(1, 274, role="negative"), rs(2, 171, role="negative"), rs(3, 14232), rs(4, 7423)]
        comp, lines = h.compare(rows)
        self.assertAlmostEqual(comp[3].x_negative, 1423.2 / 22.25, places=6)
        self.assertAlmostEqual(comp[3].sd_above, (1423.2 - 22.25) / np.std([27.4, 17.1], ddof=1), places=6)
        self.assertTrue(comp[3].above_limit)
        self.assertIsNone(comp[1].x_negative)
        self.assertEqual(lines[0], "1200 mV: 2 negative (mean 22.2/s, SD 7.3, limit 44.1/s) · 2 positive"
                                   " · weakest positive ÷ strongest negative = 27×")

    def test_compare_n1_and_no_negatives(self):
        comp, lines = h.compare([rs(1, 274, role="negative"), rs(2, 1000)])
        self.assertAlmostEqual(comp[2].x_negative, 100 / 27.4, places=6)
        self.assertIsNone(comp[2].sd_above)
        self.assertIsNone(comp[2].above_limit)
        self.assertIn("(mean 27.4/s, SD –, limit –)", lines[0])
        comp, lines = h.compare([rs(1, 500, gain=900.0)])
        self.assertIsNone(comp[1].x_negative)
        self.assertEqual(lines[0], "900 mV: 0 negative · 1 positive")

    def test_role_none_excluded(self):
        comp, lines = h.compare([rs(1, 274, role="negative"), rs(2, 171, role="negative"),
                                 rs(3, 2000, role="none"), rs(4, 7423)])
        self.assertIsNone(comp[3].x_negative)
        self.assertIn("1 positive", lines[0])

    def test_gain_unknown_group(self):
        _, lines = h.compare([rs(1, 100, gain=None, role="negative"), rs(2, 900, gain=None), rs(3, 50, gain=700.0)])
        self.assertTrue(lines[0].startswith("700 mV:"))
        self.assertTrue(lines[1].startswith("Gain unknown:"))

    def test_before_after_line(self):
        rows = [h.RowStat(1, 7, 1200.0, "none", 0.0, 2.8, 706, 2.8, trace_key="run_007.csv"),
                h.RowStat(2, 7, 1200.0, "positive", 4.0, 60.0, 79708, 56.0, trace_key="run_007.csv")]
        _, lines = h.compare(rows)
        self.assertIn("run 7: 4–60 s ÷ 0–2.8 s = 5.6× (1,423 ± 5 vs 252 ± 9 /s)", lines)

    def test_before_after_grouped_by_trace_not_run(self):
        a = h.RowStat(1, 7, 1200.0, "positive", 0.0, 15.0, 300, 15.0, trace_key="A/run_007.csv")
        b = h.RowStat(2, 7, 1200.0, "positive", 0.0, 60.0, 6000, 60.0, trace_key="B/run_007.csv")
        _, lines = h.compare([a, b])
        self.assertFalse(any(s.startswith("run 7:") for s in lines), lines)
        c = h.RowStat(3, None, None, "none", 0.0, 1.0, 10, 1.0, trace_key="C/x.csv")
        d = h.RowStat(4, None, None, "positive", 2.0, 4.0, 100, 2.0, trace_key="C/x.csv")
        _, lines = h.compare([c, d])
        self.assertTrue(any(s.startswith("x.csv: 2\u20134 s \u00f7 0\u20131 s = 5.0\u00d7") for s in lines), lines)

    def test_rate_and_err(self):
        r = rs(1, 400, duration=4.0)
        self.assertEqual(r.rate, 100.0)
        self.assertEqual(r.err, 5.0)
        self.assertEqual(rs(2, 0, duration=0.0).rate, 0.0)


class WidthHelperTests(unittest.TestCase):
    def test_whole_sample_bins_centre_on_multiples(self):
        e = h.width_edges([np.array([2.0, 4.0, 6.0])], 2.0)
        self.assertAlmostEqual(e[0], 1.0)
        self.assertAlmostEqual(e[1], 3.0)
        hist = np.histogram([2.0, 4.0, 6.0], e)[0]
        self.assertEqual(list(hist[:3]), [1, 1, 1])      # no empty bins between whole-sample widths

    def test_log_part_beyond_lin_samples(self):
        e = h.width_edges([np.array([2.0, 400.0])], 2.0)
        self.assertAlmostEqual(e[h.LIN_SAMPLES], 41.0)
        self.assertGreaterEqual(e[-1], 400.0)
        self.assertTrue(np.all(np.diff(e) > 0))

    def test_sub_half_sample_widths_get_bins(self):
        e = h.width_edges([np.array([0.3, 2.0])], 2.0)     # FWHM can be under half a sample
        self.assertLessEqual(e[0], 0.3)
        self.assertTrue(np.all(np.diff(e) > 0))

    def test_mixed_rates_10khz_widths_inside_range(self):
        e = h.width_edges([np.array([2.0, 4.0]), np.array([100.0, 200.0])], 2.0)
        self.assertEqual(np.histogram([100.0, 200.0], e)[0].sum(), 2)

    def test_non_uniform_log_only(self):
        e = h.width_edges([np.array([3.0, 30.0])], None)
        self.assertTrue(np.all(np.diff(np.log10(e)) > 0))
        self.assertLessEqual(e[0], 3.0)
        self.assertGreaterEqual(e[-1], 30.0)

    def test_all_nan_or_empty(self):
        self.assertIsNone(h.width_edges([np.array([np.nan]), np.empty(0)], 2.0))
        self.assertEqual(h.width_stats(np.array([np.nan])), (None, None, None, 0))
        self.assertEqual(h.width_stats(np.empty(0)), (None, None, None, 0))

    def test_density_units(self):
        e = np.array([1.0, 3.0])
        d = h.width_density(np.array([2.0, 2.0, np.nan]), e, 4.0)
        self.assertAlmostEqual(d[0], 2 / 4.0 / 2.0)

    def test_stats(self):
        med, q25, q75, n = h.width_stats(np.array([1.0, 2.0, 3.0, 4.0, np.nan]))
        self.assertEqual(n, 4)
        self.assertAlmostEqual(med, 2.5)
        self.assertAlmostEqual(q25, 1.75)
        self.assertAlmostEqual(q75, 3.25)

    def test_log_bins_hold_whole_samples(self):
        dt = 2.0
        e = h.width_edges([np.arange(1, 400) * dt], dt)
        k = e / dt - 0.5
        np.testing.assert_allclose(k, np.round(k), atol=1e-9)       # every edge on a half-sample boundary
        self.assertTrue(np.all(np.diff(e) > 0))

    def test_even_widths_give_flat_density(self):
        dt = 2.0
        w = np.arange(1, 400) * dt                                  # one spike at every whole-sample width
        e = h.width_edges([w], dt)
        d = h.width_density(w, e, 1.0)
        inner = d[:-1]                                              # last bin may be cut at the widest
        np.testing.assert_allclose(inner, 1 / dt, rtol=1e-9)

    def test_subsample(self):
        self.assertEqual(len(h.subsample(100)), 100)
        s = h.subsample(20000)
        self.assertEqual(len(s), h.D_MAX_POINTS)
        self.assertTrue(np.all(np.diff(s) > 0))
        np.testing.assert_array_equal(s, h.subsample(20000))   # deterministic


if __name__ == "__main__":
    unittest.main()
