# Follow main view — design (short)

## Intent
The user counts spikes in the stretch after an event (door closed/opened, substrate introduced).
They find that stretch by zooming the main chart; the Histogram and Binned windows should then
show the same time range without retyping it.

## Goals
- Histogram and Binned windows each get a **"Follow main view"** tick box, **on by default** each time
  the window opens.
- While ticked, the window's time range = the main chart's visible x-range. This updates when the main
  chart's x-range changes for any reason: Zoom ↔, typed axis limits, Back, Reset, or redrawing after
  loading runs.
- Updates wait until zooming settles (~400 ms after the last change), so a series of zoom steps
  recomputes once.

## Behaviour per window
- **Histogram:** every row's From/To = the visible range, clipped to that row's run
  (`max(lo, run start)` to `min(hi, run end)`). Spike counts, the baseline and plots recompute for the
  new range. A run that has no samples in the range shows the existing "No samples in the window" message in its row instead of
  stats, and is left out of the plot and the summary. Rows added while following take the main view
  range.
- **Binned:** the From/To boxes are filled with the visible range and the bins are recomputed.
- **Taking control by hand turns Follow off** in that window, with a status note "Follow main view
  off — range set by hand.":
  - Histogram: editing a row's From/To cell.
  - Binned: typing in From/To, or drag-selecting a span in its own chart.
  Ticking the box again snaps back to the main view range.
- Zooming *inside* the Binned overlay only changes that chart's zoom. It does not change its From/To
  and does not affect the main window, so there is no feedback loop.

## Non-goals
- Statistics and Noise keep using the main window's **Window from / to** boxes and Calculate. Zooming
  does not change those boxes.
- Event marks with event-relative ranges (option 2) are deferred until runs with different event times
  need comparing.
- The tick box state is not saved between sessions.

## Approaches considered
1. **Views follow the main chart's zoom (chosen by the user).** Small, and matches how they work.
   Limitation: one clock time for all runs.
2. Event marks with per-run ranges such as "event + 1 s to event + 10 s". Fair across runs with
   different event times, but much larger (marks stored per run, edited, drawn). Deferred.
3. Build 1 now and add 2 later. This was the recommendation; the user chose 1.

## Constraints
- The main chart already reports zoom changes through `App._on_xlim` (60 ms debounce for redraw).
  Following hooks in alongside it with its own ~400 ms debounce.
- Histogram recomputes on its worker thread. A new range cancels or supersedes any pass still running,
  so only the latest range's results are shown.
- Raw data, Statistics, Noise and the existing per-row editing stay as they are when Follow is off.

## Left to the builder
How the listener is registered (callback list on `App` vs direct calls to open windows), the exact
debounce value within 300–500 ms, and where the tick box sits in each window's top bar.

## Results (2026-11-19)
| Item | Target | Measured | Verdict |
|---|---|---|---|
| Zoom → Histogram + Binned settled, Test 015 runs 6 + 7 (10 kHz, 0.15 M + 0.6 M samples), 4–20 s | < 3 s | 1.1 s (incl. 0.4 s delay) | pass |
| Same, Test 008 run_001 + run_006 (500 kHz, 7.4 M + 7.5 M samples), 4–10 s | < 3 s | 1.0 s (incl. 0.4 s delay) | pass |
| Spike counts 4–20 s at threshold 4.6 mV | record | run 6: 264 (run ends at 15 s), run 7: 20,992 | recorded |

Build note: when the followed range reaches the shown runs' start or end, that side of the Binned
From/To stays open (empty box), so the last, shorter bin is kept.
