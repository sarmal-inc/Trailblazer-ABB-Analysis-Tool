"""Histogram window: rows, threshold, plots, table, comparison.
Run: python -m unittest tests.test_histogram_view -v"""
import gc
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import histogram  # noqa: E402
import multi_file_analyzer as m  # noqa: E402
import noise  # noqa: E402
import run_info  # noqa: E402
from matplotlib.backend_bases import MouseEvent  # noqa: E402

FS = 10_000
LSB = 0.152588


def write_run(path, run, rates, seconds=4.0, seed=0):
    """10 kHz run on the DAQ grid; rates = [(t_start, t_end, pulses per s)] of 2-sample pulses."""
    rng = np.random.default_rng(seed)
    n = int(seconds * FS)
    v = rng.normal(0.06, 0.3, n)
    for a, b, rate in rates:
        slots = np.arange(int(a * FS), int(b * FS) - 2, 4)
        k = min(len(slots), int(rate * (b - a)))
        for i in rng.choice(slots, k, replace=False):
            hgt = rng.uniform(20, 400)
            v[i:i + 2] = [hgt, hgt * 0.6]
    v = np.round(np.round(v / LSB) * LSB, 4)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# run {run}\n# experiment hist test\n# sample_rate_hz {FS}\n# pmt_gain_mv 700\n")
        f.write("time_s,value_mV\n")
        for i, x in enumerate(v):
            f.write(f"{i / FS:.4f},{x:.4f}\n")


class HistBase(unittest.TestCase):
    RUNS = ((1, [(0, 4, 20)]), (2, [(0, 4, 500)]), (3, [(0, 2, 20), (2, 4, 400)]))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.cleanup)
        self.paths = []
        for run, rates in self.RUNS:
            p = os.path.join(self.tmp.name, f"run_{run:03d}.csv")
            write_run(p, run, rates, seed=run)
            self.paths.append(p)
        self.ri_path = os.path.join(self.tmp.name, "run_info.json")
        store = {run_info.file_key(self.paths[0]): run_info.RunInfo("negative", "Negative control", 1200.0),
                 run_info.file_key(self.paths[1]): run_info.RunInfo("positive", "Positive control", 1200.0),
                 run_info.file_key(self.paths[2]): run_info.RunInfo("positive", "Positive control", 1200.0)}
        run_info.save(self.ri_path, store)
        self.app = m.App(settings_path=os.path.join(self.tmp.name, "settings.json"), run_info_path=self.ri_path)
        self.app.cache_dir = os.path.join(self.tmp.name, "cache")
        self.errors = []
        self.app.report_callback_exception = lambda *a: self.errors.append(a)
        self.app.load_paths(self.paths)
        self.wait(lambda: not self.app._loading)
        self.app.t0_var.set("0"); self.app.t1_var.set("4")

    def cleanup(self):
        a = self.app
        if a._histogram is not None:
            a._histogram.close()
        a.update()
        a._lines.clear(); a.files.clear(); a._result_files.clear(); a._res_rows.clear()
        a.destroy()
        gc.collect()
        self.tmp.cleanup()

    def wait(self, cond, limit=60):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            self.app.update()
            if cond():
                return
            time.sleep(0.01)
        self.fail("timed out")

    def open(self):
        w = self.app.open_histogram()
        self.wait(lambda: not w.busy)
        return w

    def row_of(self, w, run, t_from=None):
        return next(r for r in w.rows if m.run_number(r.trace) == run and (t_from is None or r.t_from == t_from))


class HistogramWindowTest(HistBase):
    def test_opens_with_shown_rows_and_default_threshold(self):
        w = self.open()
        self.assertEqual([m.run_number(r.trace) for r in w.rows], [1, 2, 3])
        self.assertEqual([(r.t_from, r.t_to) for r in w.rows], [(0.0, 3.9999)] * 3)
        neg = self.row_of(w, 1)                                  # the only negative row
        b, s = histogram.robust_baseline(*histogram.level_counts(
            neg.trace["data"], *neg.trace["data"].index_range(neg.t_from, neg.t_to)))
        self.assertAlmostEqual(w.threshold, round(b + 10 * s, 1))
        self.assertLess(w.threshold, 10.0)                       # not pulled up by the positive runs
        self.assertEqual(self.errors, [])

    def test_typed_threshold_updates_counts(self):
        w = self.open()
        self.assertTrue(w.set_threshold(10.0))
        self.wait(lambda: not w.busy)
        df = w.table()
        for r in w.rows:
            sp = noise.find_spikes_at(r.trace["data"], r.t_from, r.t_to, 10.0)
            got = df.loc[(df["Run"] == m.run_number(r.trace)) & (df["From (s)"] == r.t_from), "Spikes"].iloc[0]
            self.assertEqual(got, len(sp.times))
        w.thr_var.set("abc")
        w.thr_entry.focus_force()
        self.app.update()
        w.thr_entry.event_generate("<Return>")
        self.app.update()
        self.assertEqual(w.threshold, 10.0)
        self.assertEqual(str(w.thr_entry.cget("bg")).lower(), "#fecaca")

    def test_drag_threshold_line(self):
        w = self.open()
        w.update()
        w.canvas.draw()
        ax, cv = w.ax_a, w.canvas
        rel = w.threshold - w.reference
        x0, y0 = ax.transData.transform((rel, 1.0))
        y0 = ax.bbox.y0 + ax.bbox.height / 2
        inv = ax.transData.inverted()
        new_rel = inv.transform((x0 + 30, y0))[0]                          # before the redraw
        px = abs(inv.transform((x0 + 31, y0))[0] - inv.transform((x0 + 29, y0))[0]) / 2
        for name, x in (("button_press_event", x0), ("motion_notify_event", x0 + 30),
                        ("button_release_event", x0 + 30)):
            cv.callbacks.process(name, MouseEvent(name, cv, x, y0, button=1))
        self.assertLessEqual(abs(w.threshold - (w.reference + new_rel)), px)   # within one pixel
        self.assertGreater(w.threshold - w.reference, rel)
        self.wait(lambda: not w.busy)
        r = w.rows[0]
        sp = noise.find_spikes_at(r.trace["data"], r.t_from, r.t_to, w.threshold)
        self.assertEqual(w.table().loc[w.table()["Run"] == 1, "Spikes"].iloc[0], len(sp.times))

    def test_duplicate_row_before_after(self):
        w = self.open()
        r3 = self.row_of(w, 3)
        d = w.duplicate_row(r3.id)
        self.assertTrue(d.dashed)
        self.assertTrue(w.set_row_window(r3.id, 0.0, 1.9))
        self.assertTrue(w.set_row_window(d.id, 2.0, 4.0))
        self.assertFalse(w.set_row_window(d.id, 3.0, 2.0))
        w.set_row_role(r3.id, "none")
        self.wait(lambda: not w.busy)
        df = w.table()
        first = df[(df["Run"] == 3) & (df["From (s)"] == 0.0)].iloc[0]
        self.assertEqual(first["Sample"], "— (not compared)")
        self.assertEqual(first["× negative"], "–")
        self.assertTrue(any(s.startswith("run 3: 2–4 s ÷ 0–1.9 s = ") for s in w.summary_lines()),
                        w.summary_lines())

    def test_comparison_columns(self):
        w = self.open()
        df = w.table()
        rate = {int(r["Run"]): float(r["Rate (/s)"]) for _, r in df.iterrows()}
        row2 = df[df["Run"] == 2].iloc[0]
        self.assertAlmostEqual(float(row2["× negative"]), rate[2] / rate[1], places=6)
        self.assertEqual(row2["SD above negative"], "–")          # one negative: no SD
        self.assertEqual(row2["Sample"], "Positive")
        self.assertEqual(row2["Gain (mV)"], "1200")
        self.assertEqual(df[df["Run"] == 1].iloc[0]["Sample"], "Negative")

    def test_gain_from_file_when_no_override(self):
        store = run_info.load(self.ri_path)
        store[run_info.file_key(self.paths[2])] = run_info.RunInfo("positive", "", None)
        run_info.save(self.ri_path, store)
        w = self.open()
        df = w.table()
        self.assertEqual(df[df["Run"] == 3].iloc[0]["Gain (mV)"], "700 (file)")
        self.assertEqual(list(df["Run"]), [1, 2, 3])             # 1200 group first, then 700

    def test_plot_limit_12(self):
        w = self.open()
        for _ in range(11):
            w.duplicate_row(w.rows[0].id)
        self.wait(lambda: not w.busy)
        self.assertEqual(len(w.rows), 14)
        self.assertEqual(sum(r.plot for r in w.rows), 12)
        off = next(r for r in w.rows if not r.plot)
        self.assertFalse(w.set_row_plot(off.id, True))
        self.assertEqual(w.plotted_count(), (12, 12))

    def test_threshold_below_reference_message(self):
        w = self.open()
        w.set_threshold(w.reference - 1.0)
        self.wait(lambda: not w.busy)
        self.assertEqual(w.b_message(), f"Set the threshold above the reference level ({w.reference:.2f} mV)")
        self.assertGreater(w.plotted_count()[0], 0)
        self.assertGreater(int(w.table()["Spikes"].iloc[0]), 0)
        w.set_threshold(w.reference + 10)
        self.wait(lambda: not w.busy)
        self.assertEqual(w.b_message(), "")

    def test_empty_row_window(self):
        w = self.open()
        r = w.rows[0]
        self.assertTrue(w.set_row_window(r.id, 10.0, 12.0))
        self.wait(lambda: not w.busy)
        df = w.table()
        self.assertEqual(df[df["Run"] == 1].iloc[0]["Spikes"], "No samples in the window")
        self.assertEqual(w.plotted_count(), (2, 2))
        self.assertNotEqual(df[df["Run"] == 2].iloc[0]["Spikes"], "No samples in the window")

    def test_remove_trace_while_computing(self):
        real = histogram.level_counts

        def slow(*a, **kw):
            time.sleep(0.2)
            return real(*a, **kw)
        with mock.patch.object(histogram, "level_counts", side_effect=slow):
            w = self.app.open_histogram()
            self.app.update()
            self.assertTrue(w.busy)
            self.app.ftree.selection_set("1")
            self.app.remove_selected()
            self.wait(lambda: not w.busy)
        self.assertEqual([m.run_number(r.trace) for r in w.rows], [1, 3])
        self.assertEqual(len(w.table()), 2)
        self.assertEqual(self.errors, [])

    def test_toolbar_and_raise(self):
        self.assertEqual(self.app.hist_btn.cget("text"), "Histogram…")
        w = self.open()
        self.assertIs(self.app.open_histogram(), w)
        self.app.clear_all()
        self.app.update()
        self.assertIsNone(self.app._histogram)
        self.assertFalse(w.winfo_exists())



class EditingExportTest(HistBase):
    def test_edit_gain_moves_row_between_groups(self):
        w = self.open()
        r3 = self.row_of(w, 3)
        self.assertTrue(w.edit_gain(r3.id, "900"))
        df = w.table()
        self.assertEqual(df[df["Run"] == 3].iloc[0]["Gain (mV)"], "900")
        self.assertEqual(list(df["Run"]), [1, 2, 3])
        self.assertTrue(any(s.startswith("900 mV: 0 negative") for s in w.summary_lines()), w.summary_lines())
        self.assertEqual(run_info.load(self.ri_path)[run_info.file_key(self.paths[2])].gain_mv, 900.0)
        self.assertTrue(w.edit_gain(r3.id, ""))                      # empty = use the file's value
        self.assertEqual(w.table().iloc[-1]["Gain (mV)"], "700 (file)")

    def test_edit_gain_rejects_text(self):
        w = self.open()
        self.assertFalse(w.edit_gain(w.rows[0].id, "high"))
        self.assertEqual(w.table().iloc[0]["Gain (mV)"], "1200")

    def test_save_row_as_run_info_persists(self):
        w = self.open()
        r2 = self.row_of(w, 2)
        w.edit_sample(r2.id, "negative", "Buffer")
        w.save_row_as_run_info(r2.id)
        info = run_info.load(self.ri_path)[run_info.file_key(self.paths[1])]
        self.assertEqual((info.role, info.label, info.gain_mv), ("negative", "Buffer", 1200.0))
        w.close()
        w2 = self.open()
        self.assertEqual(self.row_of(w2, 2).role, "negative")

    def test_import_dialog_preview_and_apply(self):
        run_info.save(self.ri_path, {})                              # start without any run info
        xlsx = os.path.join(self.tmp.name, "Summary.xlsx")
        pd.DataFrame({"Run": [1, 2, 3], "CH": [3, 7, 7], "Conditions": [None] * 3,
                      "PMT gain": [1100, 1100, 1000]}).pipe(
            lambda s1: _write_book(xlsx, s1, pd.DataFrame({"CH": [3, 7], "Unnamed: 1": ["Negative control",
                                                                                      "Positive control"]})))
        w = self.open()
        dlg = w.open_import(xlsx)
        self.assertEqual((dlg.choice.run_col, dlg.choice.gain_col, dlg.choice.sample_col),
                         ("Run", "PMT gain", "CH"))
        self.assertEqual([p.status for p in dlg.preview], ["ok", "ok", "ok"])
        dlg.set_ticked(2, False)
        self.assertEqual(dlg.apply(), 2)
        self.app.update()
        df = w.table()
        self.assertEqual(df[df["Run"] == 1].iloc[0]["Sample"], "Negative")
        self.assertEqual(df[df["Run"] == 2].iloc[0]["Gain (mV)"], "1100")
        self.assertEqual(df[df["Run"] == 3].iloc[0]["Gain (mV)"], "700 (file)")
        self.assertEqual(len(run_info.load(self.ri_path)), 2)

    def test_import_locked_file_message(self):
        w = self.open()
        before = run_info.load(self.ri_path)
        with mock.patch.object(run_info, "read_workbook", side_effect=run_info.ImportFailed("The file is locked. Close it in Excel and try again.")), \
                mock.patch("histogram_view.messagebox.showerror") as err:
            self.assertIsNone(w.open_import(os.path.join(self.tmp.name, "x.xlsx")))
        err.assert_called_once()
        self.assertIn("The file is locked", err.call_args.args[1])
        self.assertEqual(run_info.load(self.ri_path), before)

    def test_export_csv_contents(self):
        w = self.open()
        out = os.path.join(self.tmp.name, "hist.csv")
        w.export(out)
        with open(out, encoding="utf-8") as f:
            text = f.read()
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("Plot,Run,Sample,Gain (mV)"), lines[0])
        for s in w.summary_lines():
            self.assertIn(s, text)
        self.assertIn(f"Threshold (mV),{w.threshold}", text)
        self.assertIn(f"Reference (mV),{w.reference}", text)
        self.assertIn(f"File,{self.paths[0]},Gain source,override", text)

    def test_export_xlsx_sheets(self):
        w = self.open()
        out = os.path.join(self.tmp.name, "hist.xlsx")
        w.export(out)
        book = pd.read_excel(out, sheet_name=None)
        self.assertEqual(set(book), {"Histogram", "Summary"})
        self.assertEqual(list(book["Histogram"].columns)[:3], ["Plot", "Run", "Sample"])

    def test_save_image_png(self):
        w = self.open()
        out = os.path.join(self.tmp.name, "hist.png")
        w.save_image(out)
        with open(out, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")



class ReviewFixTest(HistBase):
    def test_level_cache_not_reused_across_traces(self):
        from trace_store import TraceData
        w = self.open()
        t = np.round(np.arange(4000) * 1e-4, 4)
        wrong = []
        for k in range(20):
            data = TraceData.from_arrays(t, np.round(np.full(4000, 10.0 * k), 4))
            w._work(10_000 + k, threading.Event(), [(1, data, "x", 0.0, 1.0, None, "none")], 5.0)
            msgs = []
            while not w._q.empty():
                msgs.append(w._q.get())
            base = next(m_[3] for m_ in msgs if m_[0] == "base")
            if abs(base["baseline"] - 10.0 * k) > 1e-9:
                wrong.append((k, base["baseline"]))
            del data, msgs
            gc.collect()
        self.assertEqual(wrong, [])

    def test_drop_missing_prunes_level_cache(self):
        w = self.open()
        self.assertTrue(w._level_cache)
        self.app.ftree.selection_set("1")
        self.app.remove_selected()
        alive = {id(f["data"]) for f in self.app.files}
        self.assertTrue(all(id(v[0]) in alive for v in w._level_cache.values()))

    def test_one_failing_row_does_not_stop_others(self):
        bad = self.app.files[1]["data"]
        real = noise.find_spikes_at

        def flaky(data, *a, **kw):
            if data is bad:
                raise OSError("cache file went offline")
            return real(data, *a, **kw)
        with mock.patch.object(noise, "find_spikes_at", side_effect=flaky):
            w = self.open()
        df = w.table()
        self.assertTrue(str(df[df["Run"] == 2].iloc[0]["Spikes"]).startswith("error: "))
        self.assertIsInstance(df[df["Run"] == 1].iloc[0]["Spikes"], (int, np.integer))
        self.assertIsInstance(df[df["Run"] == 3].iloc[0]["Spikes"], (int, np.integer))

    def test_import_keeps_hand_set_row_roles(self):
        w = self.open()
        r3 = self.row_of(w, 3)
        d = w.duplicate_row(r3.id)
        w.set_row_role(d.id, "none")
        store = dict(w.store)
        store[run_info.file_key(self.paths[2])] = run_info.RunInfo("negative", "Buffer", 1200.0)
        w.store = store
        w._store_changed({run_info.file_key(self.paths[2])})
        self.assertEqual(w._row(d.id).role, "none")              # hand-set: kept
        self.assertEqual(w._row(r3.id).role, "negative")         # from run info: updated

    def test_poll_survives_refresh_error_and_close_cancels_timer(self):
        w = self.open()
        real = w._refresh
        calls = []

        def boom():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("draw failed")
            real()
        w._refresh = boom
        w.set_threshold(w.threshold + 1) if False else None
        w._start()
        self.wait(lambda: not w.busy)
        w._refresh = real
        w.set_threshold(w.threshold + 1)
        self.wait(lambda: not w.busy)                           # the poll loop is still alive
        aid = w._after_id
        w.close()
        self.assertNotIn(aid, self.app.tk.call("after", "info"))


def _write_book(path, s1, s2):
    with pd.ExcelWriter(path) as wr:
        s1.to_excel(wr, sheet_name="Sheet1", index=False)
        s2.to_excel(wr, sheet_name="Sheet2", index=False)


if __name__ == "__main__":
    unittest.main()
