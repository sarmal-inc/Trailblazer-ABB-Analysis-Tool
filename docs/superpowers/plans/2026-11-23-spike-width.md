# Spike Width Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every spike in the Histogram window gets a time-above-threshold width and an FWHM. New plots C (width histogram) and D (width vs height) and new table columns show the selected width.

**Architecture:** `noise.find_spikes_at` also returns each spike's peak index and time above threshold. A new numpy-only module, `spike_width.py`, computes FWHM from the raw samples around each peak, vectorised in chunks, with the close-together-pulse ("overlapped") rule. `histogram.py` gets the width bin-edge and statistics helpers. `histogram_view.py` computes FWHM on its worker thread against the row's robust baseline, adds the Width selector, a 2 × 2 figure, the table columns and the export.

**Tech Stack:** Python 3, numpy, matplotlib (TkAgg), Tkinter/ttk, pandas, unittest.

**Spec:** `docs/superpowers/specs/2026-11-23-spike-width-design.md`

## Global Constraints

- Spike detection, counts, rates and existing `SpikeResult` fields are unchanged. New fields have defaults, so the Noise tab is unaffected.
- FWHM height and half-max level use the row's **robust baseline** (`histogram.robust_baseline`), not `SpikeResult.baseline`.
- Overlapped rule: the walk each side is bounded by the neighbouring spike's peak (exclusive), the walk limit and the window `[i0, i1)`. If no sample below half-max is found in that range, FWHM = NaN, the spike counts as "overlapped" and is left out of C, D and the width columns in FWHM mode.
- Walk limit: `WALK_LIMIT_S = 200e-6` each side, but at least 5 samples (`max(200 µs, 5·dt)`).
- Width units shown to the user: µs.
- Plot D draws at most `D_MAX_POINTS = 5000` points per row (random subsample, fixed seed `0`), and says when subsampled. The table statistics use all spikes.
- Width computation is vectorised (no Python loop per spike). FWHM is processed in chunks of ≤ 20 000 spikes to bound memory.
- Performance: widths add ≤ ~25 % to the spike computation time for a real 500 kHz run.
- Before copying back over the working folder: archive replaced files to `Archived\2026-11-23_before_spike_width\` + README.

## Review Focus

1. **Window edge:** a spike whose peak is the first/last sample in the window, so the walk hits `i0`/`i1` before half-max. It is overlapped (NaN) and doesn't crash or index out of range. Test in Task 2.
2. **Rows with no spikes or every spike overlapped:** C/D draw nothing for that row, and the table shows "–" in the width columns, without numpy warnings or a crash. Test in Tasks 3 and 4.
3. **Mixed sample rates** (10 kHz + 500 kHz rows plotted together): C bins come from the smallest dt, and the 10 kHz row's widths land in sensible bins. Test in Task 3.
4. **Non-uniform time (`trace.dt is None`):** time above threshold uses the per-sample steps, and FWHM interpolates on the real times. Test in Tasks 1 and 2.
5. **Switching the Width selector** while the worker is mid-recompute: no recompute starts, the table and plots redraw from cached results, and stale FWHM arrays from an older generation are never shown. Test in Task 4.

---

### Task 1: Peak index and time above threshold in `find_spikes_at`

**Files:**
- Modify: `noise.py` (`SpikeResult` ~line 177, `find_spikes_at` ~line 246)
- Test: `tests/test_noise_spikes.py` (new class `SpikeWidthFieldTests`, reuse `_trace`)

**Interfaces:**
- Produces: `SpikeResult` gains `peak_idx: np.ndarray` (int64, global sample index of each spike's highest sample), `width_above: np.ndarray` (s, float64), `fwhm: np.ndarray` (s, float64; filled by Task 4, empty here). All default to `np.empty(0)` via `field(default_factory=...)`.

- [ ] **Step 1: Write the failing tests**

```python
class SpikeWidthFieldTests(unittest.TestCase):
    def test_peak_idx_and_width_above(self):
        tr = _trace(4096 * 4, spikes=[(1000, 80.0), (9000, 120.0)], hold=3)
        sp = noise.find_spikes_at(tr, 0, tr.t_end, BASE + 40)
        np.testing.assert_array_equal(sp.peak_idx, [1000, 9000])
        np.testing.assert_allclose(sp.width_above, [3 * DT, 3 * DT])
        self.assertEqual(len(sp.fwhm), 0)

    def test_gap_inside_joined_spike_not_counted(self):
        # two 2-sample bursts 10 samples apart (20 µs < 50 µs join gap): one spike, 4 samples above
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_noise_spikes.SpikeWidthFieldTests -v`
Expected: FAIL / ERROR with `AttributeError: 'SpikeResult' object has no attribute 'peak_idx'`

- [ ] **Step 3: Implement**

In `find_spikes_at`:
- `peak_idx = idx[first_max]`.
- Per-sample step: `trace.dt` when uniform, else `times_at(min(idx+1, n-1)) - times_at(idx)`, with the last sample of the trace using the previous step.
- `width_above = np.add.reduceat(step, starts)`. Pass both to `SpikeResult`.

- [ ] **Step 4: Run the tests to verify they pass, along with the existing noise tests**

Run: `python -m unittest tests.test_noise_spikes tests.test_noise_app -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add noise.py tests/test_noise_spikes.py
git commit -m "noise: spikes report peak index and time above threshold"
```

---

### Task 2: FWHM with the overlapped rule (`spike_width.py`)

**Files:**
- Create: `spike_width.py` (numpy only, no Tk)
- Test: `tests/test_spike_width.py`

**Interfaces:**
- Consumes: `SpikeResult.peak_idx` (Task 1), `TraceData.values_at(idx)`, `TraceData.times_at(idx)`, `TraceData.dt`.
- Produces: `spike_width.fwhm(trace, peak_idx, baseline, i0, i1, walk_limit_s=WALK_LIMIT_S, chunk=20_000) -> np.ndarray`. This is FWHM in seconds per spike, NaN = overlapped. `peak_idx` must be ascending. Constants `WALK_LIMIT_S = 200e-6`, `MIN_WALK_SAMPLES = 5`.

- [ ] **Step 1: Write the failing tests**

Build traces with `TraceData.from_arrays` on a flat baseline of 0 mV (no noise unless stated). `DT = 2e-6`.

```python
def test_triangle_pulse_exact():
    # values 0,50,100,50,0 around peak idx 100: half-max 50 is reached exactly at idx 99 and 101
    # -> crossings by interpolation at 99 and 101 -> FWHM = 2*DT
    ...
    np.testing.assert_allclose(spike_width.fwhm(tr, np.array([100]), 0.0, 0, tr.n), [2 * DT])

def test_interpolated_crossing():
    # 0,40,100,40,0 -> crossing between 98.x and 101.y: left at 99 + (50-40)/(100-40)... ; assert value
    # FWHM = 2*DT*(1 - 10/60)

def test_baseline_shifts_half_max():
    # same pulse on a +20 mV floor with baseline=20 gives the same FWHM as on 0 with baseline=0

def test_overlapped_neighbour_stays_above_half():
    # two 100 mV pulses 30 samples apart joined by a 70 mV plateau: both NaN
    res = spike_width.fwhm(tr, np.array([100, 130]), 0.0, 0, tr.n)
    assert np.isnan(res).all()

def test_neighbour_with_dip_below_half_is_clean():
    # same, but the plateau dips to 10 mV between them: both finite

def test_walk_limit():
    # a 100 mV pulse with a 300 µs tail at 60 mV on the right: NaN (limit 200 µs)

def test_window_edge():
    # peak at i0 (first sample of the window) -> NaN, no IndexError; peak at i1-1 -> NaN

def test_10khz_min_walk_samples():
    # dt = 100 µs: 200 µs is 2 samples, but MIN_WALK_SAMPLES=5 lets a 4-sample-wide tail resolve

def test_non_uniform_time_interpolates_on_real_times():
    # trace with dt None; expected FWHM computed by hand from the times

def test_chunking_matches_single_pass():
    # 50 random pulses; chunk=7 gives identical output to chunk=20_000

def test_empty():
    assert len(spike_width.fwhm(tr, np.empty(0, dtype=np.int64), 0.0, 0, tr.n)) == 0
```

Fill in each trace and expected value; each comment fixes the expected behaviour.

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_spike_width -v`
Expected: ERROR `ModuleNotFoundError: No module named 'spike_width'`

- [ ] **Step 3: Implement `fwhm`**

Algorithm, per chunk of peaks:
- L = walk limit in samples: `max(MIN_WALK_SAMPLES, ceil(walk_limit_s / dt))`. For `dt is None`, use the median step over `[i0, i1)`.
- Build an index matrix `peak[:,None] + arange(-L, L+1)`. Valid if the index is inside `[i0, i1)` and strictly between the previous and next peak (`peak_idx` neighbours; first/last use `i0-1` / `i1`). Read values with `values_at` on the clipped indices and mask invalid ones.
- `half = baseline + (v_peak - baseline) / 2`.
- Left side: the nearest valid column left of centre with `v < half`. Right side: likewise. If either is missing → NaN.
- Crossing time: linear interpolation between that sample and its inward neighbour on `times_at`.
- FWHM = t_right − t_left.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_spike_width -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add spike_width.py tests/test_spike_width.py
git commit -m "spike_width: vectorised FWHM with overlapped rule"
```

---

### Task 3: Width bins and statistics (`histogram.py`)

**Files:**
- Modify: `histogram.py`
- Test: `tests/test_histogram.py` (new class `WidthHelperTests`)

**Interfaces:**
- Produces:
  - `width_edges(width_sets_us: list[np.ndarray], dt_us: float | None) -> np.ndarray | None` gives bin edges in µs, or None when no finite widths.
    - With `dt_us`: whole-sample bins `(k − 0.5)·dt_us` for k = 1 … `LIN_SAMPLES` (= 20), then `PER_DECADE` log bins up to the max width.
    - With `dt_us is None`: log bins from the min to the max finite width.
    - `dt_us` is the smallest dt among the plotted rows.
  - `width_density(widths_us, edges, duration) -> np.ndarray`: spikes per s per µs (NaN widths ignored).
  - `width_stats(widths_us) -> (median, q25, q75, n_finite)`. With no finite widths it returns `(None, None, None, 0)`.
  - `D_MAX_POINTS = 5000` and `subsample(n, cap=D_MAX_POINTS, seed=0) -> np.ndarray` give sorted indices to draw.

Note: C uses **spikes per s per µs**, not per bin. The whole-sample and log bins have different widths, so a per-bin count would jump artificially where the two meet (spec anti-goal). This overrides the spec's "same normalisation as B" line; tell the user.

- [ ] **Step 1: Write the failing tests**

```python
class WidthHelperTests(unittest.TestCase):
    def test_whole_sample_bins_centre_on_multiples(self):
        e = histogram.width_edges([np.array([2.0, 4.0, 6.0])], 2.0)
        self.assertAlmostEqual(e[0], 1.0); self.assertAlmostEqual(e[1], 3.0)
        h = np.histogram([2.0, 4.0, 6.0], e)[0]
        self.assertEqual(list(h[:3]), [1, 1, 1])      # no empty bins between whole-sample widths

    def test_log_part_beyond_lin_samples(self):
        e = histogram.width_edges([np.array([2.0, 400.0])], 2.0)
        self.assertAlmostEqual(e[histogram.LIN_SAMPLES], 41.0)
        self.assertGreaterEqual(e[-1], 400.0)

    def test_mixed_rates_10khz_widths_inside_range(self):
        e = histogram.width_edges([np.array([2.0, 4.0]), np.array([100.0, 200.0])], 2.0)
        h = np.histogram([100.0, 200.0], e)[0]
        self.assertEqual(h.sum(), 2)

    def test_non_uniform_log_only(self):
        e = histogram.width_edges([np.array([3.0, 30.0])], None)
        self.assertTrue(np.all(np.diff(np.log10(e)) > 0))

    def test_all_nan_or_empty(self):
        self.assertIsNone(histogram.width_edges([np.array([np.nan]), np.empty(0)], 2.0))
        self.assertEqual(histogram.width_stats(np.array([np.nan])), (None, None, None, 0))

    def test_density_units(self):
        e = np.array([1.0, 3.0]); d = histogram.width_density(np.array([2.0, 2.0, np.nan]), e, 4.0)
        self.assertAlmostEqual(d[0], 2 / 4.0 / 2.0)

    def test_stats(self):
        self.assertEqual(histogram.width_stats(np.array([1.0, 2.0, 3.0, 4.0, np.nan]))[3], 4)

    def test_subsample(self):
        self.assertEqual(len(histogram.subsample(100)), 100)
        s = histogram.subsample(20000); self.assertEqual(len(s), 5000)
        np.testing.assert_array_equal(s, histogram.subsample(20000))   # deterministic
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_histogram.WidthHelperTests -v`
Expected: ERROR `AttributeError: module 'histogram' has no attribute 'width_edges'`

- [ ] **Step 3: Implement the four helpers and constants in `histogram.py`**

Reuse `_log_edges`. `width_stats` uses `np.nanpercentile` only when there are finite values, so it raises no warnings.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_histogram -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add histogram.py tests/test_histogram.py
git commit -m "histogram: width bin edges, density, stats, subsample"
```

---

### Task 4: Histogram window: FWHM on the worker, Width selector, plots C/D, table, export

**Files:**
- Modify: `histogram_view.py` (`COLUMNS`, `_build`, `_work`, `table`, `summary_lines`, `_draw`, `_export_tail`; the `geometry`/`figsize` values)
- Test: `tests/test_histogram_view.py` (new class `WidthViewTests(HistBase)`)

**Interfaces:**
- Consumes: `SpikeResult.peak_idx/width_above/fwhm` (Task 1), `spike_width.fwhm` (Task 2), `histogram.width_edges/width_density/width_stats/subsample/D_MAX_POINTS` (Task 3).
- Produces (public on `HistogramWindow`, used by tests):
  - `width_mode` is `"above"` (the default) or `"fwhm"`. `set_width_mode(mode)` redraws from cached results without recomputing.
  - `plotted_count()` now returns a 4-tuple `(A, B, C, D)` of drawn artist counts.
  - `d_note()` returns the subsample note text, or `""`.
  - `fwhm_overlap()` returns `(overlapped, total)` over the plotted rows.
  - `COLUMNS` gains `"Median width (µs)"`, `"Width IQR (µs)"` and `"FWHM overlapped"` after `"Max peak (mV)"`.

Behaviour:
- **Worker (`_work`):** after `find_spikes_at`, compute `fwhm = spike_width.fwhm(data, sp.peak_idx, robust_baseline, i0, i1)` and put `dataclasses.replace(sp, fwhm=fwhm)` in the "spikes" message.
  - The robust baseline is `bases[rid][0]`.
  - Errors stay per row, as today.
- **Width selector:** a readonly `ttk.Combobox` in the top bar, after the Follow main view controls. Its values are `"Time above threshold"` and `"FWHM"`. Selecting one calls `set_width_mode`.
- **Figure:** a `2 × 2` grid. `ax_a`, `ax_b` on top, `ax_c`, `ax_d` below. Raise `figsize` and window `geometry` so the table stays visible on a 1080p laptop, e.g. figure `(11, 6.2)` and window `1250x1000`, capped to the screen height.
- **Plot C**, "C. Spike widths":
  - `stairs` per row of `width_density` on `width_edges` over the plotted rows, with the same colour/dash as B.
  - Log x, log y.
  - xlabel `"time above threshold (µs) · 1 sample = {dt} µs"`, or `"FWHM (µs) · …"`. Use `"non-uniform sampling"` in place of `1 sample = …` when dt is None.
  - ylabel `"spikes per s per µs"`.
- **Plot D**, "D. Width vs height":
  - x = peak height above the reference (as B), y = the selected width (µs), log-log.
  - `scatter(s=4, alpha=0.4)` of `subsample` points per row.
  - The title gets `" · up to 5,000 per row shown"` when any row was subsampled. `d_note()` returns that text.
- **FWHM mode:** NaN widths are dropped. `summary_lines()` gets `"FWHM: {o:,} of {n:,} spikes overlapped (left out)"`.
- **Median filter on** (`getattr(self.app, "_median_applied", 0)`): C's xlabel gets `" · median-filtered ({w}-pt): pulses widened"`.
- **Table:** the width columns come from `width_stats` for the selected mode, "–" when `None`. `"FWHM overlapped"` is `"{o:,} of {n:,}"`, or "–" before spikes arrive.
- **Export:** the `_export_tail` lines add `["Width", "Time above threshold" | "FWHM"]`. Save image is unchanged (whole figure).

- [ ] **Step 1: Write the failing tests** (reuse `HistBase`'s 10 kHz runs: 2-sample pulses `[h, 0.6h]`)

```python
class WidthViewTests(HistBase):
    def test_default_mode_and_columns(self):
        w = self.open_window_with_runs()          # existing HistBase helper pattern
        self.assertEqual(w.width_mode, "above")
        df = w.table()
        for c in ("Median width (µs)", "Width IQR (µs)", "FWHM overlapped"):
            self.assertIn(c, df.columns)
        # 2-sample pulses above a threshold well below 0.6*20 mV -> 200 µs at 10 kHz
        self.assertAlmostEqual(float(df["Median width (µs)"].iloc[0]), 200.0)

    def test_four_plots_drawn(self):
        w = self.open_window_with_runs()
        a, b, c, d = w.plotted_count()
        self.assertEqual((a, b, c, d), (3, 3, 3, 3))

    def test_switch_mode_does_not_recompute(self):
        w = self.open_window_with_runs()
        with mock.patch.object(w, "_start") as st:
            w.set_width_mode("fwhm")
            st.assert_not_called()
        self.assertEqual(w.width_mode, "fwhm")
        o, n = w.fwhm_overlap()
        self.assertGreater(n, 0)
        self.assertTrue(any("overlapped" in s for s in w.summary_lines()))

    def test_switch_mode_during_recompute_shows_no_stale_fwhm(self):
        # start a recompute (threshold change), switch mode before it finishes, wait:
        # every row's fwhm has len == len(times) of the current SpikeResult

    def test_row_without_spikes(self):
        # threshold above every pulse: table width cells are "–", plotted_count C and D are 0, no warnings
        # (run with warnings.catch_warnings(); simplefilter("error", RuntimeWarning))

    def test_d_subsample_note(self):
        with mock.patch.object(histogram, "D_MAX_POINTS", 10):
            w = self.open_window_with_runs()
            self.assertIn("per row shown", w.d_note())

    def test_export_has_width_columns_and_mode(self):
        # export to csv: header contains the three new columns; tail contains ["Width", "Time above threshold"]

    def test_median_note(self):
        # app._median_applied = 3 -> C's xlabel contains "median-filtered (3-pt)"
```

Use or extend the existing helpers in `HistBase` to open the window, add the runs and wait for the worker. Match how the current `test_histogram_view` tests wait for `busy` to clear.

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_histogram_view.WidthViewTests -v`
Expected: FAIL / ERROR (no `width_mode`, the columns are missing)

- [ ] **Step 3: Implement as described under Behaviour above**

- [ ] **Step 4: Run the whole histogram view suite and the app integration tests**

Run: `python -m unittest tests.test_histogram_view tests.test_follow_view tests.test_app_integration -v`
Expected: all PASS. Existing tests that unpack `plotted_count()` as a 2-tuple must be updated to take the first two values.

- [ ] **Step 5: Commit**

```bash
git add histogram_view.py tests/test_histogram_view.py
git commit -m "histogram view: spike widths (selector, plots C/D, table, export)"
```

---

### Task 5: Real-data performance check, docs and archive

**Files:**
- Create: `tests/test_spike_width_real.py` (it skips when the real runs aren't available locally; skip cloud-only files via `trace_store.is_cloud_only`, as `tests/test_histogram_real.py` does)
- Modify: the `multi_file_analyzer.py` help text (the Histogram section). Add one paragraph covering the Width selector, plots C/D, the overlapped count, the µs columns and the effect of the Median filter.
- Modify: the spec, `docs/superpowers/specs/2026-11-23-spike-width-design.md`. Append a `## Results (2026-11-23)` table.
- Create: `Archived\2026-11-23_before_spike_width\` with the pre-change `noise.py`, `histogram.py`, `histogram_view.py` and `multi_file_analyzer.py`, plus a `README.md` (what, why, date). Take them with `git show <commit before Task 1>:<file>`, not from the edited working copies.

- [ ] **Step 1: Write the timing test.** On one real 500 kHz positive and one negative run, time `find_spikes_at` alone and then `find_spikes_at + spike_width.fwhm` (best of 3). Assert the ratio ≤ 1.25. Print the spike counts, overlapped fraction, median widths and timings.

- [ ] **Step 2: Run it**

Run: `python -m unittest tests.test_spike_width_real -v`
Expected: PASS (or SKIP if no real runs). If it fails, profile `spike_width.fwhm` (the chunk size and the `values_at` calls) before changing anything else.

- [ ] **Step 3: Record the measured numbers** in the spec's Results table (item / target / measured / verdict), as in the follow-main-view spec.

- [ ] **Step 4: Full test suite**

Run: `python -m unittest discover -s tests -v`
Expected: all PASS (real-data tests may SKIP)

- [ ] **Step 5: Archive and commit**

```bash
git add tests/test_spike_width_real.py multi_file_analyzer.py docs/superpowers/specs/2026-11-23-spike-width-design.md "Archived/2026-11-23_before_spike_width"
git commit -m "spike width: real-data timing, help text, archive"
```
