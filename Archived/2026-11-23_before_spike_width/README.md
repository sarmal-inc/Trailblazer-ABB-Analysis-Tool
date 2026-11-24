# Before spike width (2026-11-23)

Versions of the files replaced when spike widths were added to the Histogram window
(Width selector, plots C "Spike widths" and D "Width vs height", Median width / Width IQR /
FWHM overlapped columns). Taken from commit e1580b7 (main + spec/plan), before any change.

- noise.py: SpikeResult had no peak_idx / width_above / fwhm.
- histogram.py: no width_edges / width_density / width_stats / subsample.
- histogram_view.py: 1 x 2 figure (A, B) only. Note: the working copy at that time also had the
  uncommitted Median filter hook (data_changed); this archived copy is the committed version.
- multi_file_analyzer.py: help text without the "Spike widths" section. Same note as above.

New file: spike_width.py. Design: docs/superpowers/specs/2026-11-23-spike-width-design.md
