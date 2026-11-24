# Spike width — design

## Intent
The user wants to know how wide (how long) signal pulses are, for two reasons:
1. **Tell real signal from noise.** Find a width, or width + height combination, where positive
   runs differ from negative/dark runs.
2. **Research the pulse.** The real signal's characteristics are not known yet, so the tool should
   show what is there rather than assume a pulse shape.

This is phase 1 of 2. Phase 2 (later, separate design) is an **average pulse shape** view: all spikes
of a row aligned on their peak, mean waveform ± spread. Phase 1 should not block it (see Constraints).

## Goals
- Every spike found in the Histogram window gets **two widths**:
  - **Time above threshold**: number of samples above the threshold in that spike × dt. Gaps
    joined by the join gap (50 µs or 1.5 dt) are not counted. It depends on the threshold.
  - **FWHM**: full width at half the spike's height above the row's baseline. Walk left and right
    from the peak sample until the signal drops below baseline + height/2, with linear interpolation
    between the last sample above and the first sample below on each side. This is independent of
    the threshold, and the half-max crossing may lie below the threshold.
- A **Width selector** in the Histogram window's top bar picks which width the new plots and the table use:
  "Time above threshold" (default) or "FWHM".
- Two new plots, so the figure becomes a 2 × 2 grid:
  - **C. Spike widths**: histogram of the selected width, one line per plotted row (same colour/dash
    as A and B), y = spikes per s per bin (same normalisation as B). x in µs.
  - **D. Width vs height**: each spike as a point (x = peak height above reference in mV, log;
    y = selected width in µs, log), coloured by row. Noise and real pulses should show up as separate
    clusters.
- Table: new columns **"Median width (µs)"** and **"Width IQR (µs)"** (25th–75th percentile), using
  the selected width. These go into Export (CSV/xlsx) too; Export's Summary sheet records which width
  was selected. Save image saves all four plots.
- Widths are recomputed whenever spikes are (threshold change, range change, Follow main view, Median
  filter toggle).

## Non-goals
- Average pulse shape (phase 2).
- Automatic signal/noise classification or a width cut/filter on spikes. The user judges from the plots
  first. A width cut can come later once the plots show where it belongs.
- Changing the spike detection itself (threshold rules, join gap) or the counts/rates already shown.
- Width in the Noise tab or main window.

## Anti-goals
- Widths that are artefacts of sampling and look like a physical result. At 10 kHz (Test 015, dt = 100 µs) a
  pulse may be 1–2 samples wide. The plots must make that resolution visible (say the sample
  interval on the plot, e.g. "1 sample = 100 µs"), and the time-above-threshold histogram must not
  show the empty bins and spikes that come from binning whole-sample widths unevenly.
- Making the Histogram window noticeably slower for the runs it already handles.

## Approaches considered
- A. Width histogram only. Least work, but it shows that the distributions differ, not which spikes differ.
- **B. Width histogram + width-vs-height plot (chosen).** It serves both goals: the plots show whether the groups separate and how
  width grows with height.
- C. B + average pulse shape. The most direct look at the pulse, but a new kind of plot. The user chose
  "B now, C after".

## Decided
- Both widths are computed and the user switches between them; the default is time above threshold.
- **FWHM baseline (Claude's call):** FWHM's height and half-max level use the row's **robust
  baseline** (`histogram.robust_baseline`, the one the table's peak columns use), not the block
  baseline in `SpikeResult.baseline`, which is biased upward in dense luciferase positives. The robust
  baseline is passed into the width computation; spike detection and counts stay unchanged.
- **Close-together pulses (user's choice):** the FWHM walk on each side stops at the lowest sample
  between this spike's peak and the neighbouring spike's peak. If the signal has not dropped below
  half-max before that point, the spike is **"overlapped"**. It gets FWHM = NaN and is left out of C,
  D and the table's width columns in FWHM mode. The window shows the count, e.g. "FWHM: 1,240 of
  8,900 spikes overlapped" (per row in the table or tooltip, and in total under the plots).
  Spikes that reach the walk limit without a crossing are handled the same way, in the same count.
  Time above threshold is unaffected and covers every spike. Inside one joined spike the walk starts
  at the highest sample, so a dip below half-max between sub-peaks ends the FWHM there. That is accepted.
- Plots C and D are added to the existing Histogram window (same rows, threshold, Follow main view),
  not in a new window.
- Widths reflect the data as currently loaded. With the main window's Median filter on, widths are
  of the filtered signal. The status/plot note should say so when the filter is on, because filtering
  widens and flattens pulses.

## Constraints
- Spike detection lives in `noise.find_spikes_at`, which returns `SpikeResult` (peak times + heights
  only). Add per-spike width arrays to `SpikeResult` (e.g. `n_above` / `width_above`, `fwhm`) without
  changing existing fields. The Noise tab also uses `SpikeResult`, so new fields must default
  sensibly.
- `find_spikes_at` only reads raw samples for blocks whose max exceeds the threshold. The FWHM walk
  can need samples just outside those blocks (pulse tails below threshold), which must be read too.
  Limit the walk to a sensible maximum (e.g. the join gap or a few hundred µs each side). A spike
  whose half-max crossing isn't reached within the limit counts as "overlapped" (see Decided).
- Traces without a uniform dt (`trace.dt is None`): time above threshold uses the per-sample time
  steps. FWHM interpolates on the actual times.
- Scale: real runs are 500 kHz, 7.4 M samples, and positives can have very many spikes (≈17 % of
  samples above threshold in luciferase positives). Width computation must be vectorised, not a
  Python loop per spike. Plot D must cap the drawn points per row (random subsample, e.g. ≤ 5 000 per
  row, small markers, some transparency) and say when it is subsampled. Statistics in the table use all
  spikes.
- Rows computed on the worker thread as today. Widths are part of the "spikes" result for a row.
- Phase 2 will need each spike's peak index. Keep it (or make it cheap to get) in `SpikeResult`.
- Before copying back over the working folder: archive the replaced files to
  `Archived\YYYY-MM-DD_before_spike_width\` with a README (project rule).

## Performance target
Adding widths should add no more than ~25 % to the Histogram window's time to compute spikes for a real 500 kHz run
(positive and negative). Measure on real local runs, as in earlier work.

## Left to the builder
- Exact bin edges for C (whole-sample bins at small widths, log bins beyond is the suggested shape) and the
  FWHM walk limit value.
- Where the Width selector sits in the top bar; the 2 × 2 figure size, within reason for a laptop
  screen (the table must stay visible).
- Whether D's x axis uses the same reference-relative heights as B (preferred, for consistency).
- Field names in `SpikeResult`.
