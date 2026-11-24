import unittest

import numpy as np

import noise
from trace_store import TraceData

DT = 2e-6
BASE = 98.0
SIGMA = 2.0


def _trace(n, seed=0, spikes=(), hold=1, t=None, tone_rms=0.0):
    """Trace: baseline 98 mV, noise sigma 2 mV, 4-decimal values; spikes = [(index, height)]."""
    rng = np.random.default_rng(seed)
    v = BASE + rng.normal(0, SIGMA, n)
    if tone_rms:
        v += np.sqrt(2) * tone_rms * np.sin(2 * np.pi * 60.0 * np.arange(n) * DT)
    for i, h in spikes:
        v[i:i + hold] = BASE + h
    if t is None:
        t = np.round(np.arange(n) * DT, 6)
    return TraceData.from_arrays(t, np.round(v, 4))


class BlockRowsTests(unittest.TestCase):
    def test_block_rows_partition(self):
        tr = _trace(4096 * 5 + 100)
        B = tr.block
        for i0, i1 in [(10, 500), (4000, 4200), (B - 1, B + 1), (0, tr.n), (100, 3 * B + 7),
                       (B, 2 * B), (0, B)]:
            rows, head, tail = tr.block_rows(i0, i1)
            covered = list(range(*head))
            for r in rows:
                self.assertEqual(r["n"], B)
            nb = len(rows)
            if nb:
                b0 = head[1] // B
                covered += list(range(b0 * B, (b0 + nb) * B))
            covered += list(range(*tail))
            self.assertEqual(covered, list(range(i0, i1)), (i0, i1))


class SpikeTests(unittest.TestCase):
    def test_known_spikes(self):
        n = 4096 * 12 + 300
        idx = [1500, 2500, 4096 * 3, 4096 * 3 + 200, 4096 * 5 + 17, 9000, 20000, 30000, 40000, 49000]
        idx += [4096 * 7 + 100 + 100 * j for j in range(40)]
        heights = np.linspace(30, 200, len(idx))
        tr = _trace(n, spikes=list(zip(idx, heights)))
        self.assertEqual(len(idx), 50)
        i0 = 1000  # inside block 0: partial head block
        res = noise.find_spikes(tr, float(tr.times_at(i0)), tr.t_end, 10.0)
        expect = np.sort(np.array(idx))
        self.assertEqual(len(res.times), 50)
        np.testing.assert_array_equal(res.times, tr.times_at(expect))
        np.testing.assert_allclose(res.heights, tr.values_at(expect) - res.baseline, atol=1e-9)

    def test_boundary_spanning_spike_is_one(self):
        tr = _trace(4096 * 4, spikes=[(4094, 60), (4095, 80), (4096, 150), (4097, 70)], hold=1)
        res = noise.find_spikes(tr, tr.t_start, tr.t_end, 10.0)
        self.assertEqual(len(res.times), 1)
        self.assertEqual(res.times[0], tr.times_at(4096))

    def test_wobble_across_block_edge_is_one(self):
        # 20 samples = 40 us gap across the block edge at 4096
        tr = _trace(4096 * 4, spikes=[(4090, 100), (4110, 150)])
        res = noise.find_spikes(tr, tr.t_start, tr.t_end, 10.0)
        self.assertEqual(len(res.times), 1)
        self.assertEqual(res.times[0], tr.times_at(4110))

    def test_wobble_is_one_spike(self):
        # three crossings within 40 us (20 samples); a second spike 60 us (30 samples) later
        tr = _trace(20000, spikes=[(5000, 100), (5008, 120), (5016, 90), (5046, 100)])
        res = noise.find_spikes(tr, tr.t_start, tr.t_end, 10.0)
        self.assertEqual(len(res.times), 2)
        self.assertEqual(res.times[0], tr.times_at(5008))
        # exactly 60 us after the last sample above threshold of the first
        tr = _trace(20000, spikes=[(5000, 100), (5030, 100)])
        self.assertEqual(len(noise.find_spikes(tr, tr.t_start, tr.t_end, 10.0).times), 2)

    def test_baseline_robust(self):
        n = 4096 * 200
        rng = np.random.default_rng(5)
        blocks = rng.choice(200, size=2, replace=False)  # 1 % of the blocks
        spikes = [(int(b) * 4096 + 1000, 500.0) for b in blocks]
        tr = _trace(n, spikes=spikes, hold=4)
        base, sigma = noise.spike_baseline(tr, 0, n)
        self.assertAlmostEqual(base, BASE, delta=0.05)
        self.assertAlmostEqual(sigma, SIGMA, delta=0.1)

    def test_baseline_no_whole_block(self):
        tr = _trace(4096 * 3)
        base, sigma = noise.spike_baseline(tr, 100, 3000)
        self.assertAlmostEqual(base, BASE, delta=0.2)
        self.assertAlmostEqual(sigma, SIGMA, delta=0.2)
        base, sigma = noise.spike_baseline(tr, 100, 130)  # shorter than a 64 chunk pair
        self.assertTrue(np.isfinite(base) and np.isfinite(sigma))

    def test_flat_signal(self):
        n = 20000
        tr = TraceData.from_arrays(np.round(np.arange(n) * DT, 6), np.full(n, 5.0))
        res = noise.find_spikes(tr, tr.t_start, tr.t_end, 10.0)
        self.assertEqual(res.sigma, 0.0)
        self.assertEqual(res.threshold, 5.0)
        self.assertEqual(len(res.times), 0)

    def test_rate_and_spacing(self):
        n = 500_000
        tr = _trace(n, spikes=[(2000 + 5000 * j, 100) for j in range(100)])
        r = noise.analyze(tr, tr.t_start, tr.t_end, [], 10.0, 2.0)
        self.assertEqual(r.n_spikes, 100)
        self.assertAlmostEqual(r.rate, 100.0, delta=1.0)
        self.assertAlmostEqual(r.median_spacing, 0.010, places=9)
        self.assertAlmostEqual(r.spacing_hz, 100.0, delta=1e-6)


class AnalyzeTests(unittest.TestCase):
    def test_analyze_nonuniform(self):
        n = 20000
        rng = np.random.default_rng(1)
        t = np.round(np.arange(n) * DT + rng.uniform(0, 0.5e-6, n), 7)
        t = np.maximum.accumulate(t)
        tr = _trace(n, spikes=[(3000, 100), (9000, 100)], t=t)
        self.assertIsNone(tr.dt)
        r = noise.analyze(tr, tr.t_start, tr.t_end, [60.0, 120.0], 10.0, 2.0)
        self.assertEqual(r.peaks, [])
        self.assertEqual(r.watch, {60.0: None, 120.0: None})
        self.assertEqual(r.note, "Spectrum needs evenly spaced samples.")
        self.assertEqual(r.n_spikes, 2)
        self.assertIsNone(r.resolution_hz)
        self.assertEqual(r.seen, [])
        self.assertIsNone(r.n_slices)

    def test_analyze_empty_window(self):
        tr = _trace(10000)
        r = noise.analyze(tr, 5.0, 6.0, [60.0], 10.0, 2.0)
        self.assertEqual(r.note, "No samples in the window")
        self.assertEqual(r.n_spikes, 0)
        self.assertEqual(r.peaks, [])
        self.assertEqual(r.watch, {60.0: None})
        self.assertEqual(r.seen, [])
        self.assertIsNone(r.n_slices)

    def test_analyze_tiny_windows_are_empty(self):
        tr = _trace(10000)
        for t0, t1 in ((5.0, 6.0), (0.0, 0.0), (0.0, 1.5e-6)):     # 0 samples, 0 or 1 sample
            i0, i1 = tr.index_range(t0, t1)
            self.assertLess(i1 - i0, 2)
            r = noise.analyze(tr, t0, t1, [60.0], 10.0, 2.0)
            self.assertEqual(r.note, noise.NOTE_EMPTY)
            self.assertEqual((r.n_spikes, r.rate, r.peaks), (0, 0.0, []))

    def test_analyze_full(self):
        n = 600_000
        tr = _trace(n, spikes=[(50000, 100), (300000, 150)], tone_rms=1.0)
        r = noise.analyze(tr, tr.t_start, tr.t_end, [60.0, 300000.0], 10.0, 2.0)
        self.assertEqual(r.note, "")
        self.assertAlmostEqual(r.peaks[0][0], 60.0, delta=1.0)
        self.assertAlmostEqual(r.watch[60.0], 1.0, delta=0.05)
        self.assertIsNone(r.watch[300000.0])
        self.assertAlmostEqual(r.resolution_hz, 500000 / 2 ** 19, places=6)
        self.assertEqual(r.n_spikes, 2)
        self.assertEqual(len(r.spikes.times), 2)
        self.assertLessEqual(len(r.peaks), noise.N_PEAKS)
        self.assertEqual(len(r.seen), len(r.peaks))
        self.assertEqual(r.n_slices, 1)                      # 1.2 s run: one 2^19 segment
        self.assertTrue(all(v is None for v in r.seen))      # one slice: "seen" is not judged

    def test_analyze_multi_slice_seen(self):
        n = 3_000_000                                        # 6 s -> 5 slices
        tr = _trace(n, tone_rms=1.0)
        r = noise.analyze(tr, tr.t_start, tr.t_end, [], 10.0, 2.0)
        self.assertEqual(r.n_slices, 5)
        self.assertAlmostEqual(r.peaks[0][0], 60.0, delta=1.0)
        self.assertEqual(r.seen[0], 5)                       # the 60 Hz tone is always on


class BlockAlignedWindowTests(unittest.TestCase):
    def test_block_aligned_window_without_candidates(self):
        tr = _trace(4096 * 4)
        sp = noise.find_spikes(tr, tr.t_start, tr.t_end, 50)
        self.assertEqual(len(sp.times), 0)


def _trace_10k(values):
    """10 kHz trace with the given mV values (4 decimals)."""
    v = np.round(np.asarray(values, dtype=float), 4)
    t = np.round(np.arange(len(v)) * 1e-4, 4)
    return TraceData.from_arrays(t, v)


class GroupingTests(unittest.TestCase):
    def _flat(self, n=20000, seed=1):
        return np.random.default_rng(seed).normal(0.0, 0.05, n)

    def test_join_gap(self):
        self.assertAlmostEqual(noise.join_gap(1e-4), 1.5e-4)
        self.assertAlmostEqual(noise.join_gap(2e-6), 50e-6)
        self.assertAlmostEqual(noise.join_gap(None), 50e-6)

    def test_consecutive_samples_join_at_10khz(self):
        v = self._flat()
        v[5000:5003] = [50.0, 80.0, 60.0]
        tr = _trace_10k(v)
        sp = noise.find_spikes_at(tr, tr.t_start, tr.t_end, 5.0)
        self.assertEqual(len(sp.times), 1)
        self.assertAlmostEqual(sp.times[0], 0.5001, places=9)
        self.assertAlmostEqual(sp.heights[0], 80.0 - sp.baseline, places=3)
        self.assertEqual(sp.threshold, 5.0)

    def test_gap_of_two_samples_splits_at_10khz(self):
        v = self._flat()
        v[5000] = 50.0
        v[5002] = 60.0
        tr = _trace_10k(v)
        sp = noise.find_spikes_at(tr, tr.t_start, tr.t_end, 5.0)
        self.assertEqual(len(sp.times), 2)

    def test_find_spikes_equals_find_spikes_at(self):
        n = 200_000
        tr = _trace(n, spikes=[(3000, 100), (90000, 150), (150000, 60)], hold=4)
        a = noise.find_spikes(tr, tr.t_start, tr.t_end, 10.0)
        b = noise.find_spikes_at(tr, tr.t_start, tr.t_end, a.baseline + 10.0 * a.sigma)
        np.testing.assert_array_equal(a.times, b.times)
        np.testing.assert_array_equal(a.heights, b.heights)
        self.assertEqual(len(a.times), 3)


class SpikeWidthFieldTests(unittest.TestCase):
    def test_peak_idx_and_width_above(self):
        tr = _trace(4096 * 4, spikes=[(1000, 80.0), (9000, 120.0)], hold=3)
        sp = noise.find_spikes_at(tr, 0, tr.t_end, BASE + 40)
        np.testing.assert_array_equal(sp.peak_idx, [1000, 9000])
        np.testing.assert_allclose(sp.width_above, [3 * DT, 3 * DT])
        self.assertEqual(len(sp.fwhm), 0)

    def test_gap_inside_joined_spike_not_counted(self):
        # two 2-sample bursts 10 samples apart (20 us < 50 us join gap): one spike, 4 samples above
        tr = _trace(4096 * 2, spikes=[(2000, 90.0), (2010, 100.0)], hold=2)
        sp = noise.find_spikes_at(tr, 0, tr.t_end, BASE + 40)
        self.assertEqual(len(sp.times), 1)
        np.testing.assert_allclose(sp.width_above, [4 * DT])

    def test_non_uniform_time(self):
        n = 4096 * 2
        t = np.round(np.cumsum(np.where(np.arange(n) % 2, 2e-6, 3e-6)), 6)
        tr = _trace(n, spikes=[(3000, 90.0)], hold=2, t=t)
        self.assertIsNone(tr.dt)
        sp = noise.find_spikes_at(tr, 0, tr.t_end, BASE + 40)
        steps = np.diff(t)
        np.testing.assert_allclose(sp.width_above, [steps[3000] + steps[3001]], rtol=1e-6)

    def test_empty_result_has_empty_arrays(self):
        tr = _trace(4096)
        sp = noise.find_spikes_at(tr, 0, tr.t_end, BASE + 1000)
        self.assertEqual((len(sp.peak_idx), len(sp.width_above)), (0, 0))


if __name__ == "__main__":
    unittest.main()
