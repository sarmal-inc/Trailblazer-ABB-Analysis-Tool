"""Binned window: each shown run's bin-averaged signal overlaid on one plot, with the main chart's
toolbar (zoom, Back, cursors) on it. See docs/superpowers/specs/2026-11-18-binned-view-design.md."""
import csv
import math
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import binning
from chart_tools import ChartTools
from trace_store import TraceData

CARD = "#ffffff"
MUTED = "#4d5967"
BAD_BG = "#fecaca"
OK_BG = "#ffffff"
NO_RUNS = "Show a run in the main window to bin it."
LEGEND_HIDDEN = "Legend hidden for more than 24 runs."
MAX_PANELS = 8


class BinnedWindow(tk.Toplevel):
    WORKER_MIN = 1_000_000      # runs with at least this many samples are binned on a worker thread

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.bin_s = 0.5
        self.t_from = None       # None = earliest run start
        self.t_to = None         # None = end of the longest run
        self._workers = set()    # tokens of running worker threads (busy while any)
        self._inflight = {}      # id(trace data) -> (trace data, bin_s) queued on a worker
        self._failed = {}        # (id(trace data), bin_s) -> trace data that could not be binned
        self._drawn_sig = None
        self.series = {}         # id(trace dict) -> (centres, means) after From/To filtering
        self._cache = {}         # (id(trace data), bin_s) -> (trace data, centres, means)
        self._runs = []          # shown trace dicts, in sort order
        self._known = set()      # ids of all loaded trace dicts at the last refresh
        self._pseudo = {}        # id(trace dict) -> trace-like dict over the binned series
        self._cancel = threading.Event()
        self._q = queue.Queue()
        self._closed = False
        self._after_id = None
        self._messages = {}
        self._refit_pending = True
        self.panel_axes = []
        self.title("Binned")
        self.geometry("1150x820")
        self.configure(background=CARD)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self.refresh(refit=True)
        self._after_id = self.after(50, self._poll)

    # ---------- layout ----------
    def _entry(self, parent, text, label, width=8):
        ttk.Label(parent, text=label, style="Card.TLabel").pack(side=tk.LEFT, padx=(0, 4))
        var = tk.StringVar(master=self, value=text)
        e = tk.Entry(parent, textvariable=var, width=width, relief=tk.SOLID, bd=1, bg=OK_BG)
        e.pack(side=tk.LEFT, padx=(0, 10))
        return var, e

    def _build(self):
        top = ttk.Frame(self, style="Card.TFrame", padding=(12, 10, 12, 4))
        top.pack(fill=tk.X)
        self.bin_var, self.bin_entry = self._entry(top, "0.5", "Bin")
        ttk.Label(top, text="s", style="Card.TLabel").pack(side=tk.LEFT, padx=(0, 14))
        self.from_var, self.from_entry = self._entry(top, "", "From")
        self.to_var, self.to_entry = self._entry(top, "", "To")
        ttk.Label(top, text="s", style="Card.TLabel").pack(side=tk.LEFT, padx=(0, 14))
        self.bin_entry.bind("<Return>", lambda ev: self.set_bin(self.bin_var.get()))
        for e in (self.from_entry, self.to_entry):
            e.bind("<Return>", lambda ev: self.set_range(self.from_var.get(), self.to_var.get()))
        self.raw_var = tk.BooleanVar(master=self, value=True)
        ttk.Checkbutton(top, text="Raw panels", style="Card.TCheckbutton", variable=self.raw_var,
                        command=lambda: self.set_raw_panels(self.raw_var.get())).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(top, text="Export\u2026", command=self._on_export).pack(side=tk.RIGHT)
        self.controls = top
        self.status_lbl = ttk.Label(self, text="", style="Muted.Card.TLabel", padding=(12, 0))
        self.status_lbl.pack(fill=tk.X)

        self.fig = Figure(figsize=(10, 6), dpi=100, facecolor=CARD)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.tools = ChartTools(self, self.ax, self.canvas, on_window_selected=self._on_span,
                                on_reset=self._on_reset, autoscale_y=self._autoscale_y,
                                get_selected_trace=self._selected_pseudo,
                                get_shown_traces=lambda: [self._pseudo[id(f)] for f in self._runs
                                                          if id(f) in self._pseudo],
                                label_of=lambda f: f["name"])
        self.tools.frame.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(2, 6))
        self.tools.readout_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=12)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 0))

    # ---------- inputs ----------
    def _status(self, key, text):
        if text:
            self._messages[key] = text
        else:
            self._messages.pop(key, None)
        self.status_lbl.config(text="  ·  ".join(self._messages.values()))

    def status_text(self):
        return str(self.status_lbl.cget("text"))

    def set_bin(self, text):
        try:
            b = float(str(text).strip())
        except ValueError:
            b = math.nan
        for f in self._runs:
            msg = binning.check_bin(f["data"], b, f.get("file", f.get("name", "")))
            if msg:
                self.bin_entry.config(bg=BAD_BG)
                self._status("bin", msg)
                return False
        if not (math.isfinite(b) and b > 0):
            self.bin_entry.config(bg=BAD_BG)
            self._status("bin", binning.MSG_POSITIVE)
            return False
        self.bin_entry.config(bg=OK_BG)
        self._status("bin", "")
        self._status("error", "")
        if b != self.bin_s:                      # stop binning for the old size
            self._cancel.set()
            self._cancel = threading.Event()
            self._inflight.clear()
        self.bin_s = b
        self.bin_var.set(f"{b:g}")
        self.refresh(refit=True)
        return True

    def set_range(self, from_text, to_text):
        def num(s):
            s = str(s).strip()
            if not s:
                return None
            v = float(s)
            if not math.isfinite(v):
                raise ValueError
            return v
        try:
            a, b = num(from_text), num(to_text)
        except ValueError:
            self.from_entry.config(bg=BAD_BG)
            return False
        if a is not None and b is not None and a >= b:
            self.from_entry.config(bg=BAD_BG)
            return False
        self.from_entry.config(bg=OK_BG)
        self.t_from, self.t_to = a, b
        self.from_var.set("" if a is None else f"{a:g}")
        self.to_var.set("" if b is None else f"{b:g}")
        self.refresh(refit=True)
        return True

    def _on_span(self, lo, hi):
        self.set_range(f"{lo:.6g}", f"{hi:.6g}")

    def _on_reset(self):
        self._fit_view()
        self.canvas.draw_idle()

    # ---------- computing ----------
    @property
    def busy(self):
        return bool(self._workers)

    def _cached(self, data):
        hit = self._cache.get((id(data), self.bin_s))
        return (hit[1], hit[2]) if hit is not None and hit[0] is data else None

    def _failed_now(self, data):
        hit = self._failed.get((id(data), self.bin_s))
        return hit is not None and hit is data

    def refresh(self, refit=None):
        """Re-read the shown runs from the app; bin what is missing; redraw only when something
        shown changed. Loading or removing runs (or refit=True) refits the view, otherwise zoom
        and cursors are kept. Runs already being binned are not started again; a run that failed
        is not retried until the bin size changes."""
        if self._closed:
            return
        known = {id(f) for f in self.app.files}
        if refit is None:
            refit = known != self._known
        self._known = known
        self._refit_pending = self._refit_pending or refit
        alive = {id(f["data"]) for f in self.app.files}
        self._cache = {k: v for k, v in self._cache.items() if id(v[0]) in alive}
        self._failed = {k: v for k, v in self._failed.items() if id(v) in alive}
        self._inflight = {k: v for k, v in self._inflight.items() if id(v[0]) in alive}
        self._runs = [f for f in self.app.files if f.get("show")]
        todo = {}
        for f in self._runs:
            d = f["data"]
            busy_same = id(d) in self._inflight and self._inflight[id(d)][1] == self.bin_s
            if self._cached(d) is None and not self._failed_now(d) and not busy_same:
                todo.setdefault(id(d), (d, f.get("file", f.get("name", ""))))
        big = []
        for d, name in todo.values():
            if d.n >= self.WORKER_MIN:
                big.append((d, name))
                continue
            try:
                self._store(d, self.bin_s, *binning.bin_means(d, self.bin_s))
            except Exception as e:
                self._fail(d, self.bin_s, name, e)
        if big:
            self._start(big)
        if not self.busy:
            self._redraw_if_changed()

    def _store(self, data, bin_s, c, m):
        self._cache[(id(data), bin_s)] = (data, c, m)

    def _fail(self, data, bin_s, name, err):
        self._failed[(id(data), bin_s)] = data
        self._status("error", f"Couldn't bin {name}: {err!r}")

    def _start(self, items):
        token, cancel, b = object(), self._cancel, self.bin_s
        self._workers.add(token)
        for d, _ in items:
            self._inflight[id(d)] = (d, b)
        n = len(items)

        def work():                                       # never touches Tk
            try:
                for i, (d, name) in enumerate(items, 1):
                    if cancel.is_set():
                        return
                    self._q.put(("progress", token, i, n, name))
                    try:
                        self._q.put(("bins", token, d, b, binning.bin_means(d, b)))
                    except Exception as e:
                        self._q.put(("fail", token, d, b, (name, e)))
            finally:
                self._q.put(("done", token))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        if self._closed:
            return
        try:
            finished = False
            alive = {id(f["data"]) for f in self.app.files}
            while True:
                try:
                    msg = self._q.get_nowait()
                except queue.Empty:
                    break
                kind = msg[0]
                if kind == "progress":
                    self._status("busy", f"Binning {msg[2]}/{msg[3]} \u00b7 {msg[4]}")
                elif kind in ("bins", "fail"):
                    _, _, d, b, payload = msg
                    if self._inflight.get(id(d), (None,))[0] is d and self._inflight[id(d)][1] == b:
                        del self._inflight[id(d)]
                    if id(d) in alive:
                        if kind == "bins":
                            self._store(d, b, *payload)
                        else:
                            self._fail(d, b, *payload)
                elif kind == "done":
                    self._workers.discard(msg[1])
                    finished = True
            if finished and not self.busy:
                self._status("busy", "")
                self.refresh(refit=False)
        finally:
            if not self._closed:
                self._after_id = self.after(50, self._poll)

    # ---------- drawing ----------
    def _filtered(self, f):
        hit = self._cached(f["data"])
        if hit is None:
            return None
        c, m = hit
        lo = -math.inf if self.t_from is None else self.t_from
        hi = math.inf if self.t_to is None else self.t_to
        keep = (c >= lo) & (c <= hi)
        return c[keep], m[keep]

    def _extent(self):
        """x range to fit: From/To, else the runs' time span widened to every bin centre (the
        last, shorter bin's centre can lie beyond the last sample)."""
        if not self._runs:
            return 0.0, 1.0
        lo = min(f["data"].t_start for f in self._runs) if self.t_from is None else self.t_from
        hi = max(f["data"].t_end for f in self._runs) if self.t_to is None else self.t_to
        cs = [s[0] for s in self.series.values() if len(s[0])]
        if cs and self.t_from is None:
            lo = min(lo, min(float(c[0]) for c in cs))
        if cs and self.t_to is None:
            hi = max(hi, max(float(c[-1]) for c in cs))
        return lo, hi if hi > lo else lo + 1.0

    def _signature(self):
        label_of = self.app._label
        return (self.bin_s, self.t_from, self.t_to, bool(self.raw_var.get()),
                tuple((id(f), label_of(f), f.get("color"), self._cached(f["data"]) is not None,
                       (f.get("meta") or {}).get("started"), (f.get("meta") or {}).get("position_mm"))
                      for f in self._runs))

    def _redraw_if_changed(self):
        if self._refit_pending or self._signature() != self._drawn_sig:
            self._redraw()

    def _redraw(self):
        if self.busy:
            return
        ax = self.ax
        keep_view = not self._refit_pending and bool(self.series)   # nothing to keep when it was empty
        xl, yl = ax.get_xlim(), ax.get_ylim()
        ax.clear()
        self.series, self._pseudo = {}, {}
        label_of = self.app._label
        for f in self._runs:
            s = self._filtered(f)
            if s is None or len(s[0]) == 0:
                continue
            self.series[id(f)] = s
            label = label_of(f)
            ax.plot(s[0], s[1], color=f.get("color"), lw=1.6, label=label,
                    marker="o" if len(s[0]) < 2 else None, ms=4)     # a single bin is a dot
            self._pseudo[id(f)] = {"data": TraceData.from_arrays(s[0], s[1]), "name": label,
                                   "color": f.get("color"), "file": f.get("file", "")}
        n = len(self.series)
        self._status("legend", LEGEND_HIDDEN if n > 24 else "")
        if n == 0:
            ax.text(0.5, 0.5, NO_RUNS, ha="center", va="center", transform=ax.transAxes, color=MUTED)
        elif n <= 24:
            ax.legend(loc="upper right", fontsize=8, ncols=1 if n <= 12 else 2, frameon=False)
        ax.set_xlabel("time (s)")
        ax.set_ylabel(f"mean mV (bin {self.bin_s:g} s)")
        ax.grid(True, color="#e7ebef", lw=0.8)
        if keep_view:
            ax.set_xlim(xl); ax.set_ylim(yl)
        else:
            self._fit_view()
            self.tools.clear_history()
        self._refit_pending = False
        self._draw_panels()
        try:
            self.fig.tight_layout()
        except Exception:
            pass
        self.tools.attach()
        self.canvas.draw_idle()
        self._drawn_sig = self._signature()

    # ---------- raw panels ----------
    def set_raw_panels(self, on):
        self.raw_var.set(bool(on))
        self._redraw_keep()

    def _redraw_keep(self):
        refit, self._refit_pending = self._refit_pending, False
        self._redraw()
        self._refit_pending = refit

    def _panel_title(self, f, a, b):
        st = f["data"].stats(a, b)
        meta = f.get("meta") or {}
        parts = [self.app._label(f)]
        if meta.get("started"):
            parts.append(f"started {meta['started']}")
        if meta.get("position_mm"):
            parts.append(f"position {meta['position_mm']} mm")
        if st["Points"]:
            parts.append(f"mean {st['Mean']:.2f} mV, max {st['Max']:.0f} mV")
        return " \u00b7 ".join(parts)

    def _draw_panels(self):
        for pa in self.panel_axes:
            pa.remove()
        self.panel_axes = []
        runs = [f for f in self._runs if id(f) in self.series]
        shown = runs[:MAX_PANELS] if self.raw_var.get() else []
        self._status("panels", f"Showing raw panels for the first {MAX_PANELS} of {len(runs)} runs."
                     if self.raw_var.get() and len(runs) > MAX_PANELS else "")
        k = len(shown)
        gs = self.fig.add_gridspec(k + 1, 1, height_ratios=[1] * k + [max(2, k // 2 + 1)])
        self.ax.set_subplotspec(gs[k])
        for i, f in enumerate(shown):
            d = f["data"]
            a = d.t_start if self.t_from is None else max(self.t_from, d.t_start)
            b = d.t_end if self.t_to is None else min(self.t_to, d.t_end)
            pa = self.fig.add_subplot(gs[i])
            t, v, _ = d.view(a, b, bins=1500)
            pa.plot(t, v, color=f.get("color"), lw=0.6)
            pa.set_xlim(a, b)
            pa.set_title(self._panel_title(f, a, b), loc="left", fontsize=8)
            pa.tick_params(labelsize=7)
            pa.set_ylabel("mV", fontsize=7)
            pa.grid(True, color="#e7ebef", lw=0.6)
            self.panel_axes.append(pa)

    # ---------- export ----------
    def export(self, path):
        """Write the binned series as CSV; refused (False) while binning is still running, so the
        header never describes bins the file doesn't hold."""
        if self.busy:
            self._status("export", "Binning in progress \u2014 export when it finishes.")
            return False
        self._status("export", "")
        lo, hi = self._extent()
        runs = [f for f in self._runs if id(f) in self.series]
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(f"# bin_s {self.bin_s:g}, from {lo:g}, to {hi:g}\n")
            if not runs:
                return True
            wr = csv.writer(fh)
            wr.writerow([h for f in runs for h in (f"{self.app._label(f)} time (s)",
                                                   f"{self.app._label(f)} mean (mV)")])
            longest = max(len(self.series[id(f)][0]) for f in runs)
            for i in range(longest):
                row = []
                for f in runs:
                    c, m = self.series[id(f)]
                    row += [repr(float(c[i])), repr(float(m[i]))] if i < len(c) else ["", ""]
                wr.writerow(row)
        return True

    def save_image(self, path):
        animated = [a for a in self.ax.get_children() if getattr(a, "get_animated", lambda: False)()]
        for a in animated:                       # cursors are blitted; savefig skips animated artists
            a.set_animated(False)
        try:
            self.fig.savefig(path, dpi=150, facecolor=CARD)
        finally:
            for a in animated:
                a.set_animated(True)

    def _on_export(self):
        path = filedialog.asksaveasfilename(parent=self, title="Export binned data", defaultextension=".csv",
                                            initialfile="binned.csv", filetypes=[("CSV", "*.csv")])
        if path:
            try:
                if not self.export(path):
                    messagebox.showinfo("Export", "Binning is still running \u2014 export when it finishes.",
                                        parent=self)
            except OSError as e:
                messagebox.showerror("Export", f"Couldn't write the file: {e}", parent=self)

    def _fit_view(self):
        lo, hi = self._extent()
        self.ax.set_xlim(lo, hi)
        fit = self._autoscale_y(lo, hi)
        if fit is not None:
            self.ax.set_ylim(*fit)

    def _autoscale_y(self, x0, x1):
        vals = [m[(c >= x0) & (c <= x1)] for c, m in self.series.values()]
        vals = [v for v in vals if len(v)]
        if not vals:
            return None
        lo, hi = min(float(v.min()) for v in vals), max(float(v.max()) for v in vals)
        pad = (hi - lo) * 0.05 or abs(hi) * 0.05 or 1.0
        return lo - pad, hi + pad

    def overlay_lines(self):
        return [ln for ln in self.ax.get_lines() if not ln.get_label().startswith("_")]

    def _selected_pseudo(self):
        for f in self.app._selected_files():
            if id(f) in self._pseudo:
                return self._pseudo[id(f)]
        return None

    # ---------- closing ----------
    def close(self):
        if self._closed:
            return
        self._closed = True
        self._cancel.set()
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
        if getattr(self.app, "_binned", None) is self:
            self.app._binned = None
        self.destroy()
