# Follow main view Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Histogram and Binned windows get a "Follow main view" tick box (on by default). While it is
ticked, the window's time range tracks the main chart's visible x-range.

**Architecture:** The `App` keeps a debounced "main view changed" push. `_refresh_view` (the existing
zoom/pan hook) and the end of `App.draw` schedule it, and it calls `follow_view(lo, hi)` on each open
window. Each window owns its tick box, its own clipping of the range, and switching follow off when the
user sets a range by hand.

**Tech Stack:** Python 3.13, tkinter/ttk, matplotlib, stdlib `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-19-follow-main-view-design.md`

**Workspace:** the project folder is now a git repo (`main`).
- Create a worktree on a new branch: `git worktree add <scratchpad>/work4 -b follow-main-view`.
- Keep files CRLF.
- Tests: `python -m unittest discover -s tests`.
- Copy back only after the user's OK: archive the replaced files in
  `Archived\2026-11-19_before_follow_main_view\` with a README, then merge the branch into `main`.
- Do not push without asking.

## Global Constraints

- The tick box text is exactly `Follow main view`, ticked whenever a window opens; its state is not saved.
- The range followed is the main chart's visible x-range (`App.ax.get_xlim()`), not the toolbar
  Window from/to.
- Debounce: `FOLLOW_DELAY_MS = 400` after the last change.
- Hand-set note, exactly: `Follow main view off — range set by hand.`
- Histogram rows are clipped per run: `max(lo, run start)` to `min(hi, run end)`. A run with no overlap
  keeps the raw `(lo, hi)`, so the existing `No samples in the window` path handles it.
- Statistics, Noise, and the toolbar Window from/to are untouched.

## Review Focus

1. **The main chart is zoomed while the Histogram worker is mid-pass:** only the latest range's results
   appear. Pinned in Task 2 (`test_zoom_during_pass_latest_wins`).
2. **A run lies entirely outside the zoomed range:** its row shows `No samples in the window` and is
   left out of the plot and summary, with no exception. Pinned in Task 2 (`test_run_outside_range`).
3. **Zooming inside the Binned overlay:** this does not change Binned From/To, the follow state, or the
   main chart. Pinned in Task 3 (`test_overlay_zoom_is_local`).
4. **A window is closed before the debounced push fires:** no exception, and nothing is pushed to the
   closed window. Pinned in Task 1 (`test_closed_window_not_pushed`).
5. **The main chart redraws with an unchanged x-range** (toggling show, renaming): the Histogram does
   not recompute and Binned does not re-bin. Pinned in Task 2 (`test_same_range_no_recompute`) and
   Task 3 (`test_same_range_no_refresh`).

---

### Task 1: App — main view push

**Files:** Modify `multi_file_analyzer.py`; Test `tests/test_follow_view.py` (new)

**Interfaces:**
- Produces:
  - `FOLLOW_DELAY_MS = 400` (module constant).
  - `App.main_view_range() -> tuple[float, float] | None`: the main axes x-limits when at least one
    run is drawn, else `None`.
  - `App._schedule_follow()`: restarts a single `after` job; it is called at the end of
    `_refresh_view` and at the end of `draw`, including the early return when no runs are shown.
  - `App._push_follow()`: for each of `self._histogram` and `self._binned` that is not `None` and not
    closed (`_closed` is False), calls `w.follow_view(lo, hi)`. It does nothing when
    `main_view_range()` is `None`.
- Windows consume `follow_view(lo: float, hi: float) -> None`. Tasks 2 and 3 implement it.

- [ ] **Step 1: Failing tests.** The fixture is the same 3-CSV app setup as `tests/test_binned_view.py`.
  - A fake window object records `follow_view` calls and has `_closed = False`. Install it as
    `app._histogram`.
  - `test_zoom_pushes_once_after_delay`: call `app.tools.zoom_x(1, 2)`, then `zoom_x(1.5, 2.5)`. Pump
    `update()` for 0.6 s. Exactly one call, with `(1.5, 2.5)` to 1e-9.
  - `test_no_push_before_delay`: zoom, then pump 0.2 s → 0 calls.
  - `test_reset_pushes_full_view`: zoom, then `app.tools.reset()`, pump 0.6 s. The last call equals
    `app.ax.get_xlim()`.
  - `test_no_runs_no_push`: hide every run and `draw()`, pump 0.6 s → 0 calls.
  - `test_closed_window_not_pushed` (Review Focus 4): zoom, set the fake's `_closed = True`, pump
    0.6 s → 0 calls and no exception.
- [ ] **Step 2: Run** `python -m unittest tests.test_follow_view -v` → fails (`AttributeError` /
  0 calls).
- [ ] **Step 3: Implement** the Interfaces above. `clear_all` cancels a pending follow job.
- [ ] **Step 4: Run** the new tests and the full suite → PASS.
- [ ] **Step 5: Commit** `app: debounced main-view push to open windows`.

---

### Task 2: Histogram follows

**Files:** Modify `histogram_view.py`; Test `tests/test_histogram_view.py` (new class `FollowTest`)

**Interfaces:**
- Consumes: Task 1 `App.main_view_range()` and the `follow_view` call.
- Produces:
  - `follow_var` (`tk.BooleanVar`, True) with a `Follow main view` checkbutton in the top bar.
  - `follow_view(lo, hi)`: ignored when unticked. It sets each row's window by the clipping rule, drops
    that row's cached base and spikes only when its window changed, and calls
    `_changed(recompute=True)` only if some row changed.
  - `set_follow(on: bool)`: on → clears the note and calls `follow_view(*app.main_view_range())` when
    a range is available. off → just unticks.
  - `follow_note() -> str`: the hand-set note text, or "".
  - Behaviour changes:
    - `set_row_window` (the hand edit path) unticks follow and sets the note.
    - `_add_row` with follow on uses `main_view_range()` (clipped), falling back to the current
      behaviour when it is `None`.

- [ ] **Step 1: Failing tests:**
  - `test_follow_on_by_default_rows_take_view`: zoom the main chart to (1, 2) and pump 0.6 s. Every
    row has `(t_from, t_to) == (1, 2)` and the stats recompute (wait not busy). Each row's Spikes
    equals the count from `noise.find_spikes_at` over (1, 2) at the window's threshold.
  - `test_clip_to_run`: one run is 0–4 s. Zoom to (-1, 2) → that row is `(0, 2)`.
  - `test_run_outside_range` (Review Focus 2): load a 4th run of 1 s. Zoom to (2, 3). Its row shows
    `No samples in the window`, it is not among the plotted lines, and the summary is computed from the
    other rows. No exception.
  - `test_hand_edit_turns_follow_off`: `set_row_window(rid, 0.5, 1)` → `follow_var.get()` is False
    and `follow_note() == "Follow main view off — range set by hand."`. A later zoom leaves the rows
    unchanged.
  - `test_retick_snaps_back`: after the hand edit, `set_follow(True)` → every row equals the clipped
    current main view, and the note is "".
  - `test_zoom_during_pass_latest_wins` (Review Focus 1): patch `histogram.level_counts` to be slow.
    Zoom to (1, 2), pump 0.6 s, then zoom to (2, 3) while busy and wait until not busy. Every row is
    (2, 3) and its stats match a direct computation for (2, 3).
  - `test_same_range_no_recompute` (Review Focus 5): after settling, wrap `_start`, toggle a run's
    alias, call `app.draw()`, pump 0.6 s → `_start` was not called.
  - `test_added_row_uses_view`: zoom to (1, 2), then `add_shown_runs()` after showing a hidden run →
    the new row is (1, 2).
- [ ] **Step 2: Run** `python -m unittest tests.test_histogram_view -v` → `FollowTest` fails.
- [ ] **Step 3: Implement** the Interfaces above.
- [ ] **Step 4: Run** `tests.test_histogram_view` and the full suite → PASS. Existing tests that assume
  rows start at the toolbar window: rule on them in the ledger. Follow on by default changes the
  initial range.
- [ ] **Step 5: Commit** `histogram: follow main view`.

---

### Task 3: Binned follows

**Files:** Modify `binned_view.py`; Test `tests/test_binned_view.py` (new class `FollowTest`)

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - `follow_var` (True) with a `Follow main view` checkbutton after "Raw panels".
  - `set_range(from_text, to_text, by_hand=True) -> bool`. With `by_hand=True` and a valid range it
    unticks follow and sets the note. The Return key and `_on_span` use `by_hand=True`.
  - `follow_view(lo, hi)`: ignored when unticked. It clips to `[min start, max end]` of the shown runs
    and calls `set_range(..., by_hand=False)` only when the result differs from the current
    `(t_from, t_to)`.
  - `set_follow(on)` and `follow_note()`, as in Task 2.

- [ ] **Step 1: Failing tests:**
  - `test_follow_sets_from_to`: zoom the main chart to (1, 3) and pump 0.6 s. The From/To boxes read
    "1" and "3", and every overlay centre is in [1, 3].
  - `test_follow_clipped`: zoom to (-1, 2) → `t_from == 0`.
  - `test_typed_range_turns_follow_off`: `set_range("0.5", "1")` → follow off and the note is set. A
    later main zoom leaves From/To unchanged.
  - `test_span_turns_follow_off`: `_on_span(1, 2)` → follow off.
  - `test_overlay_zoom_is_local` (Review Focus 3): `w.tools.zoom_x(1, 1.5)` and pump 0.6 s. `t_from`
    and `t_to` are unchanged, follow is still on, and `app.ax.get_xlim()` is unchanged.
  - `test_same_range_no_refresh` (Review Focus 5): after settling, wrap `w.refresh` and trigger an
    `app.draw()` with the same view. `follow_view` does not call `set_range`. Assert via a
    `set_range` wrapper with call count 0.
  - `test_retick_snaps_back`: after a typed range, `set_follow(True)` → From/To equal the clipped main
    view.
- [ ] **Step 2: Run** `python -m unittest tests.test_binned_view -v` → `FollowTest` fails.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests and the full suite → PASS. Existing tests that expect a full
  From/To on open: rule in the ledger (follow on fills From/To with the main view).
- [ ] **Step 5: Commit** `binned: follow main view`.

---

### Task 4: Real-data check and results

**Files:** Modify spec (append `## Results`).

- [ ] **Step 1:** Open the app on Test 015 runs 6 and 7 (paths from `tests/test_histogram_real.py`;
  skip if not local).
  - Open Histogram and Binned, then zoom the main chart to 4–20 s.
  - Time from zoom release to both windows settled. Target < 3 s for two 7.4 M-sample-class runs.
  - Record the run 6 and run 7 spike counts for 4–20 s.
- [ ] **Step 2:** Append the results (item, target, measured, verdict) to the spec, then run the full
  suite → OK.
- [ ] **Step 3: Commit** `follow main view: real-data check and results`.
