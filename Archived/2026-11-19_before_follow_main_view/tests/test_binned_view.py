"""Binned window. Run: python -m unittest tests.test_binned_view -v"""
import gc
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import binned_view  # noqa: E402
import binning  # noqa: E402
import multi_file_analyzer as m  # noqa: E402

FS = 10_000
BAD = "#fecaca"


def write_run(path, run, level, step_after=None, seconds=4.0, seed=0, meta=True):
    rng = np.random.default_rng(seed)
    n = int(seconds * FS)
    v = level + rng.normal(0, 0.5, n)
    if step_after is not None:
        v[int(step_after * FS):] += 10.0
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# run {run}\n# experiment binned test\n# sample_rate_hz {FS}\n")
        if meta:
            f.write("# position_mm -13.2585 -59.3259 2.56486\n")
        f.write("time_s,value_mV\n")
        for i, x in enumerate(v):
            f.write(f"{i / FS:.4f},{x:.4f}\n")


class BinBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.cleanup)
        self.paths = []
        for run, level, step in ((1, 0.0, None), (2, 10.0, None), (3, 20.0, 2.0)):
            p = os.path.join(self.tmp.name, f"run_{run:03d}.csv")
            write_run(p, run, level, step, seed=run)
            self.paths.append(p)
        self.app = m.App(settings_path=os.path.join(self.tmp.name, "settings.json"),
                         run_info_path=os.path.join(self.tmp.name, "ri.json"))
        self.app.cache_dir = os.path.join(self.tmp.name, "cache")
        self.errors = []
        self.app.report_callback_exception = lambda *a: self.errors.append(a)
        self.app.load_paths(self.paths)
        self.wait(lambda: not self.app._loading)

    def cleanup(self):
        a = self.app
        if a._binned is not None:
            a._binned.close()
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
        w = self.app.open_binned()
        self.wait(lambda: not w.busy)
        return w

    def is_red(self, e):
        return str(e.cget("bg")).lower() == BAD


class BinnedWindowTest(BinBase):
    def test_opens_with_one_line_per_shown_run(self):
        w = self.open()
        lines = w.overlay_lines()
        self.assertEqual([ln.get_label() for ln in lines], [self.app._label(f) for f in self.app.files])
        self.assertEqual([ln.get_color() for ln in lines], [f["color"] for f in self.app.files])
        self.assertTrue(all(len(ln.get_xdata()) == 8 for ln in lines))
        c, mns = binning.bin_means(self.app.files[2]["data"], 0.5)
        np.testing.assert_allclose(lines[2].get_ydata(), mns)
        self.assertEqual(self.errors, [])

    def test_bin_change_updates_lines(self):
        w = self.open()
        self.assertTrue(w.set_bin("0.25"))
        self.wait(lambda: not w.busy)
        self.assertTrue(all(len(ln.get_xdata()) == 16 for ln in w.overlay_lines()))
        self.assertFalse(w.set_bin("0"))
        self.assertTrue(self.is_red(w.bin_entry))
        self.assertEqual(w.bin_s, 0.25)
        self.assertFalse(w.set_bin("0.00001"))
        self.assertIn("Bin too small for run_001.csv (dt 0.0001 s).", w.status_text())
        self.assertTrue(all(len(ln.get_xdata()) == 16 for ln in w.overlay_lines()))

    def test_range_filter(self):
        w = self.open()
        self.assertTrue(w.set_range("1", "3"))
        self.wait(lambda: not w.busy)
        for ln in w.overlay_lines():
            x = np.asarray(ln.get_xdata())
            self.assertTrue(np.all((x >= 1) & (x <= 3)))
            self.assertEqual(len(x), 4)
        self.assertFalse(w.set_range("3", "1"))
        self.assertTrue(self.is_red(w.from_entry))

    def test_hidden_run_added_on_show(self):
        w = self.open()
        self.app.files[1]["show"] = False
        self.app.draw()
        self.wait(lambda: not w.busy)
        self.assertEqual(len(w.overlay_lines()), 2)
        self.app.files[1]["show"] = True
        self.app.draw()
        self.wait(lambda: not w.busy)
        self.assertEqual(len(w.overlay_lines()), 3)

    def test_cursor_reads_bin_mean(self):
        w = self.open()
        w.tools.set_mode("cursors")
        w.tools.set_cursor("A", 2.25)
        c, mns = binning.bin_means(self.app.files[0]["data"], 0.5)
        k = int(np.argmin(np.abs(c - 2.25)))
        self.assertIn(f"A: 2.250000 s, {mns[k]:.4f} mV", w.tools.readout_text())

    def test_bin_change_refits_keeps_cursors(self):
        w = self.open()
        w.tools.set_mode("cursors")
        w.tools.set_cursor("A", 1.0)
        w.tools.set_cursor("B", 3.0)
        w.tools.zoom_x(1.0, 2.0)
        self.assertTrue(w.set_bin("0.25"))
        self.wait(lambda: not w.busy)
        self.assertEqual(w.tools.cursor_times, (1.0, 3.0))
        lo, hi = w.ax.get_xlim()
        self.assertAlmostEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 3.9999)
        self.assertEqual(self.errors, [])

    def test_refresh_keeps_zoom(self):
        w = self.open()
        w.tools.zoom_x(1.0, 2.0)
        self.app.files[0]["alias"] = "renamed"
        self.app.draw()
        self.wait(lambda: not w.busy)
        self.assertEqual(tuple(w.ax.get_xlim()), (1.0, 2.0))
        self.assertEqual(w.overlay_lines()[0].get_label(), "renamed")

    def test_select_window_sets_from_to_only(self):
        w = self.open()
        before = self.app.t0_var.get()
        w.tools.set_mode("select")
        w.tools._on_drag(1.0, 2.0)
        self.wait(lambda: not w.busy)
        self.assertEqual((w.t_from, w.t_to), (1.0, 2.0))
        self.assertEqual(self.app.t0_var.get(), before)

    def test_legend_columns(self):
        w = self.open()
        base = self.app.files[0]
        for i in range(10):
            f = dict(base); f["name"] = f"x{i}"; self.app.files.append(f)
        self.app.draw()
        self.wait(lambda: not w.busy)
        self.assertEqual(w.ax.get_legend()._ncols, 2)
        for i in range(12):
            f = dict(base); f["name"] = f"y{i}"; self.app.files.append(f)
        self.app.draw()
        self.wait(lambda: not w.busy)
        self.assertIsNone(w.ax.get_legend())
        self.assertIn("Legend hidden for more than 24 runs.", w.status_text())
        del self.app.files[3:]
        self.app.draw()

    def test_remove_while_binning(self):
        real = binning.bin_means

        def slow(*a, **kw):
            time.sleep(0.2)
            return real(*a, **kw)
        with mock.patch.object(binned_view.BinnedWindow, "WORKER_MIN", 0), \
                mock.patch.object(binning, "bin_means", side_effect=slow):
            w = self.app.open_binned()
            self.app.update()
            self.assertTrue(w.busy)
            self.app.ftree.selection_set("1")
            self.app.remove_selected()
            self.wait(lambda: not w.busy)
        self.assertEqual(len(w.overlay_lines()), 2)
        self.assertEqual(self.errors, [])

    def test_toolbar_open_raise_clear(self):
        self.assertEqual(self.app.bin_btn.cget("text"), "Binned…")
        w = self.open()
        self.assertIs(self.app.open_binned(), w)
        self.app.clear_all()
        self.app.update()
        self.assertIsNone(self.app._binned)
        self.assertFalse(w.winfo_exists())



class PanelsExportTest(BinBase):
    def test_raw_panels_shown_and_hidden(self):
        w = self.open()
        w.tools.set_mode("cursors")
        self.assertTrue(w.raw_var.get())
        self.assertEqual(len(w.panel_axes), 3)
        self.assertEqual(len(w.fig.axes), 4)
        w.set_raw_panels(False)
        self.assertEqual(w.panel_axes, [])
        self.assertEqual(len(w.fig.axes), 1)
        self.assertEqual(len(w.overlay_lines()), 3)
        self.assertEqual(len(w.tools._cursor_lines), 2)
        w.set_raw_panels(True)
        self.assertEqual(len(w.fig.axes), 4)

    def test_panel_title(self):
        w = self.open()
        f = self.app.files[0]
        st = f["data"].stats(f["data"].t_start, f["data"].t_end)
        label = self.app._label(f)
        self.assertEqual(w.panel_axes[0].get_title(loc="left"),
                         f"{label} · position -13.2585 -59.3259 2.56486 mm · "
                         f"mean {st['Mean']:.2f} mV, max {st['Max']:.0f} mV")
        f["meta"]["started"] = "2026-11-17 14:47:02"
        self.app.draw()
        self.wait(lambda: not w.busy)
        self.assertIn(f"{label} · started 2026-11-17 14:47:02 · position", w.panel_axes[0].get_title(loc="left"))

    def test_eight_panel_limit(self):
        w = self.open()
        base = self.app.files[0]
        for i in range(7):
            g = dict(base); g["name"] = f"x{i}"; self.app.files.append(g)
        self.app.draw()
        self.wait(lambda: not w.busy)
        self.assertEqual(len(w.panel_axes), 8)
        self.assertIn("Showing raw panels for the first 8 of 10 runs.", w.status_text())
        del self.app.files[3:]
        self.app.draw()

    def test_export_csv(self):
        import csv
        w = self.open()
        out = os.path.join(self.tmp.name, "binned.csv")
        w.export(out)
        with open(out, encoding="utf-8", newline="") as fh:
            first = fh.readline().rstrip("\r\n")
            rows = list(csv.reader(fh))
        self.assertEqual(first, "# bin_s 0.5, from 0, to 3.9999")
        labels = [self.app._label(f) for f in self.app.files]
        self.assertEqual(rows[0], [h for lb in labels for h in (f"{lb} time (s)", f"{lb} mean (mV)")])
        c, mns = w.series[id(self.app.files[2])]
        self.assertEqual(len(rows) - 1, len(c))
        np.testing.assert_allclose([float(r[5]) for r in rows[1:]], mns, rtol=1e-9)
        np.testing.assert_allclose([float(r[4]) for r in rows[1:]], c, rtol=1e-9)

    def test_no_shown_runs(self):
        w = self.open()
        for f in self.app.files:
            f["show"] = False
        self.app.draw()
        self.wait(lambda: not w.busy)
        self.assertTrue(any(t.get_text() == binned_view.NO_RUNS for t in w.ax.texts))
        self.assertEqual(w.panel_axes, [])
        out = os.path.join(self.tmp.name, "empty.csv")
        w.export(out)
        with open(out, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("# bin_s 0.5"))

    def test_save_image_png(self):
        w = self.open()
        out = os.path.join(self.tmp.name, "binned.png")
        w.save_image(out)
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")



class ReviewFixTest(BinBase):
    def slow(self, delay=0.15, fail_on=None, calls=None):
        real = binning.bin_means

        def f(data, b):
            if calls is not None:
                calls.append(id(data))
            time.sleep(delay)
            if fail_on is not None and data is fail_on:
                raise MemoryError("out of memory")
            return real(data, b)
        return f

    def test_failing_run_no_retry_loop(self):
        calls = []
        bad = self.app.files[1]["data"]
        with mock.patch.object(binned_view.BinnedWindow, "WORKER_MIN", 0), \
                mock.patch.object(binning, "bin_means", side_effect=self.slow(0.0, bad, calls)):
            w = self.app.open_binned()
            self.wait(lambda: not w.busy, limit=10)
            for _ in range(20):
                self.app.update(); time.sleep(0.02)
        self.assertLessEqual(calls.count(id(bad)), 1)
        self.assertEqual(len(w.overlay_lines()), 2)
        self.assertIn("Couldn't bin run_002.csv", w.status_text())
        self.assertFalse(w.busy)

    def test_export_refused_while_busy(self):
        with mock.patch.object(binned_view.BinnedWindow, "WORKER_MIN", 0), \
                mock.patch.object(binning, "bin_means", side_effect=self.slow(0.3)):
            w = self.app.open_binned()
            self.app.update()
            self.assertTrue(w.busy)
            out = os.path.join(self.tmp.name, "busy.csv")
            self.assertFalse(w.export(out))
            self.assertFalse(os.path.exists(out))
            self.assertIn("Binning in progress", w.status_text())
            self.wait(lambda: not w.busy)
            self.assertTrue(w.export(out))

    def test_progress_status(self):
        seen = []
        with mock.patch.object(binned_view.BinnedWindow, "WORKER_MIN", 0), \
                mock.patch.object(binning, "bin_means", side_effect=self.slow(0.2)):
            w = self.app.open_binned()
            end = time.monotonic() + 10
            while w.busy and time.monotonic() < end:
                self.app.update(); seen.append(w.status_text()); time.sleep(0.01)
        self.assertTrue(any(f"Binning {i}/3 · run_00{i}.csv" in s for s in seen for i in (1, 2, 3)), set(seen))
        self.assertNotIn("Binning", w.status_text())

    def test_show_run_from_empty_state_fits(self):
        for f in self.app.files:
            f["show"] = False
        self.app.draw()
        w = self.open()
        self.app.files[2]["show"] = True
        self.app.draw()
        self.wait(lambda: not w.busy)
        ln = w.overlay_lines()[0]
        (x0, x1), (y0, y1) = w.ax.get_xlim(), w.ax.get_ylim()
        x, y = np.asarray(ln.get_xdata()), np.asarray(ln.get_ydata())
        self.assertTrue(np.all((x >= x0) & (x <= x1)) and np.all((y >= y0) & (y <= y1)), (x0, x1, y0, y1))

    def test_redraw_skipped_when_nothing_changed(self):
        w = self.open()
        with mock.patch.object(w, "_redraw", wraps=w._redraw) as rd:
            self.app.draw()
            self.app.draw()
            self.wait(lambda: not w.busy)
            self.assertEqual(rd.call_count, 0)
            self.app.files[0]["alias"] = "renamed"
            self.app.draw()
            self.assertEqual(rd.call_count, 1)

    def test_main_redraw_does_not_restart_binning(self):
        calls = []
        with mock.patch.object(binned_view.BinnedWindow, "WORKER_MIN", 0), \
                mock.patch.object(binning, "bin_means", side_effect=self.slow(0.15, calls=calls)):
            w = self.app.open_binned()
            self.app.update()
            for _ in range(3):
                self.app.draw(); self.app.update(); time.sleep(0.05)
            self.wait(lambda: not w.busy)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(w.overlay_lines()), 3)

    def test_single_bin_run_visible(self):
        p = os.path.join(self.tmp.name, "run_004.csv")
        write_run(p, 4, 5.0, seconds=0.3, seed=4)
        self.app.load_paths([p])
        self.wait(lambda: not self.app._loading)
        w = self.open()
        ln = next(l for l in w.overlay_lines() if len(l.get_xdata()) == 1)
        self.assertNotIn(ln.get_marker(), (None, "None", "", " "))

    def test_fit_includes_last_partial_bin(self):
        w = self.open()
        self.assertTrue(w.set_bin("0.3"))
        self.wait(lambda: not w.busy)
        last = max(float(np.max(l.get_xdata())) for l in w.overlay_lines())
        self.assertGreater(last, 3.9999)
        self.assertGreaterEqual(w.ax.get_xlim()[1], last)


if __name__ == "__main__":
    unittest.main()
