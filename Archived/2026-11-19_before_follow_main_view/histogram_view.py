"""Histogram window: intensity histograms of every sample (A) and of spike peaks (B) for a table of
rows (a trace + a time window), one shared spike threshold, and positive-vs-negative comparison at
matched PMT gain. See docs/superpowers/specs/2026-11-18-intensity-histogram-design.md."""
import math
import os
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk

import numpy as np
import pandas as pd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import histogram
import noise
import run_info

CARD = "#ffffff"
MUTED = "#4d5967"
THR_COLOR = "#111827"
BAD_BG = "#fecaca"
OK_BG = "#ffffff"
GRAB_PX = 6
DASH = "–"
NO_SAMPLES = "No samples in the window"
ROLE_TEXT = {"negative": "Negative", "positive": "Positive", "none": "— (not compared)"}
COLUMNS = ["Plot", "Run", "Sample", "Gain (mV)", "From (s)", "To (s)", "Baseline (mV)", "Spikes",
           "Rate (/s)", "±", "Median peak (mV)", "Max peak (mV)", "× negative",
           "SD above negative", "Above limit"]


def default_threshold(bases):
    """bases = [((baseline, sigma, error), role)]: the highest baseline + 10 sigma among the negative
    rows, else the lowest among all rows (the quietest), rounded to 0.1 mV; 5.0 with no data."""
    ok = [(b + 10 * s, role) for (b, s, err), role in bases if not err and b is not None]
    neg = [v for v, role in ok if role == "negative"]
    if neg:
        return round(max(neg), 1)
    return round(min(v for v, _ in ok), 1) if ok else 5.0


@dataclass
class Row:
    id: int
    trace: dict
    t_from: float
    t_to: float
    role: str          # "negative", "positive" or "none"
    plot: bool
    dashed: bool = False
    label: str = ""
    role_manual: bool = False   # role set by hand on this row: an import leaves it alone


class HistogramWindow(tk.Toplevel):
    def __init__(self, app, run_number, fmt):
        super().__init__(app)
        self.app = app
        self._run_number = run_number
        self._fmt = fmt
        self.store = run_info.load(app.run_info_path)
        self.rows = []
        self._next_id = 1
        self._base = {}           # row id -> dict(baseline, sigma, levels, counts, duration) or dict(error)
        self._spk = {}            # row id -> SpikeResult at the current threshold
        self._level_cache = {}    # (id(trace data), i0, i1) -> (trace data, levels, counts)
        self.threshold = None
        self.reference = 0.0
        self.busy = False
        self._gen = 0
        self._cancel = threading.Event()
        self._q = queue.Queue()
        self._closed = False
        self._dragging = False
        self._drag_rel = None
        self._a_artists, self._b_artists = [], []
        self._b_msg = ""
        self.thr_line_a = None
        self._after_id = None
        self.title("Histogram")
        self.geometry("1250x860")
        self.configure(background=CARD)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        for f in app.files:
            if f.get("show"):
                self._add_row(f)
        self._refresh_table()
        self._start()
        self._after_id = self.after(50, self._poll)

    # ---------- layout ----------
    def _build(self):
        top = ttk.Frame(self, style="Card.TFrame", padding=(12, 10, 12, 4))
        top.pack(fill=tk.X)
        ttk.Label(top, text="Threshold", style="Card.TLabel").pack(side=tk.LEFT)
        self.thr_var = tk.StringVar(value="")
        self.thr_entry = tk.Entry(top, textvariable=self.thr_var, width=10, relief=tk.SOLID, bd=1, bg=OK_BG)
        self.thr_entry.pack(side=tk.LEFT, padx=(6, 4))
        self.thr_entry.bind("<Return>", lambda ev: self._on_thr_entry())
        ttk.Label(top, text="mV", style="Card.TLabel").pack(side=tk.LEFT)
        self.ref_lbl = ttk.Label(top, text="", style="Muted.Card.TLabel")
        self.ref_lbl.pack(side=tk.LEFT, padx=(10, 0))
        self.status_lbl = ttk.Label(top, text="", style="Muted.Card.TLabel")
        self.status_lbl.pack(side=tk.RIGHT)

        self.fig = Figure(figsize=(11, 3.8), dpi=100, facecolor=CARD)
        self.ax_a, self.ax_b = self.fig.subplots(1, 2)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=12)
        for name, h in (("button_press_event", self._on_press), ("motion_notify_event", self._on_motion),
                        ("button_release_event", self._on_release)):
            self.canvas.mpl_connect(name, h)

        mid = ttk.Frame(self, style="Card.TFrame", padding=(12, 4, 12, 0))
        mid.pack(fill=tk.BOTH)
        self.tree = ttk.Treeview(mid, columns=COLUMNS, show="headings", height=8, selectmode="extended")
        for c in COLUMNS:
            self.tree.heading(c, text=c, anchor="w" if c in ("Run", "Sample") else "e")
            self.tree.column(c, width=150 if c == "Sample" else 88, anchor="w" if c in ("Run", "Sample") else "e",
                             stretch=False)
        xs = ttk.Scrollbar(mid, orient=tk.HORIZONTAL, command=self.tree.xview)
        ys = ttk.Scrollbar(mid, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(xscrollcommand=xs.set, yscrollcommand=ys.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        mid.columnconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self._on_tree_click)

        self.summary_lbl = ttk.Label(self, text="", style="Card.TLabel", justify=tk.LEFT, padding=(12, 4))
        self.summary_lbl.pack(fill=tk.X)
        self.btns = ttk.Frame(self, style="Card.TFrame", padding=(12, 4, 12, 10))
        self.btns.pack(fill=tk.X)
        ttk.Button(self.btns, text="Duplicate row", command=self._on_duplicate).pack(side=tk.LEFT)
        ttk.Button(self.btns, text="Remove row", command=self._on_remove).pack(side=tk.LEFT, padx=6)
        ttk.Button(self.btns, text="Add shown runs", command=self.add_shown_runs).pack(side=tk.LEFT)
        ttk.Button(self.btns, text="Save image\u2026", command=self._on_save_image).pack(side=tk.RIGHT)
        ttk.Button(self.btns, text="Export\u2026", command=self._on_export).pack(side=tk.RIGHT, padx=6)
        ttk.Button(self.btns, text="Import run info\u2026", command=self.open_import).pack(side=tk.RIGHT)
        self.tree.bind("<Double-1>", self._on_tree_double)
        self.tree.bind("<Button-3>", self._on_tree_menu)

    # ---------- rows ----------
    def _plotted(self):
        return sum(r.plot for r in self.rows)

    def _info(self, trace):
        return self.store.get(run_info.file_key(trace["path"]))

    def _add_row(self, trace, t_from=None, t_to=None, role=None, dashed=False, after=None):
        d = trace["data"]
        if t_from is None:
            rng = self.app._get_range(quiet=True) or (d.t_start, d.t_end)
            t_from, t_to = max(rng[0], d.t_start), min(rng[1], d.t_end)
        info = self._info(trace)
        if role is None:
            role = (info.role if info and info.role else "none")
        row = Row(self._next_id, trace, float(t_from), float(t_to), role, self._plotted() < histogram.MAX_PLOTTED,
                  dashed, info.label if info else "")
        self._next_id += 1
        if after is None:
            self.rows.append(row)
        else:
            self.rows.insert(self.rows.index(after) + 1, row)
        return row

    def _row(self, row_id):
        return next(r for r in self.rows if r.id == row_id)

    def duplicate_row(self, row_id):
        src = self._row(row_id)
        row = self._add_row(src.trace, src.t_from, src.t_to, src.role, dashed=True, after=src)
        self._changed(recompute=True)
        return row

    def remove_row(self, row_id):
        self.rows = [r for r in self.rows if r.id != row_id]
        self._base.pop(row_id, None)
        self._spk.pop(row_id, None)
        self._changed()

    def add_shown_runs(self):
        have = {id(r.trace) for r in self.rows}
        n = 0
        for f in self.app.files:
            if f.get("show") and id(f) not in have:
                self._add_row(f)
                n += 1
        if n:
            self._changed(recompute=True)
        return n

    def set_row_window(self, row_id, t_from, t_to):
        try:
            a, b = float(t_from), float(t_to)
        except (TypeError, ValueError):
            return False
        if not (math.isfinite(a) and math.isfinite(b)) or a >= b:
            return False
        row = self._row(row_id)
        row.t_from, row.t_to = a, b
        self._base.pop(row_id, None)
        self._spk.pop(row_id, None)
        self._changed(recompute=True)
        return True

    def set_row_role(self, row_id, role):
        if role not in ROLE_TEXT:
            raise ValueError(f"unknown role {role!r}")
        row = self._row(row_id)
        row.role, row.role_manual = role, True
        self._changed()

    def set_row_plot(self, row_id, on):
        row = self._row(row_id)
        if on and not row.plot and self._plotted() >= histogram.MAX_PLOTTED:
            return False
        row.plot = bool(on)
        self._changed()
        return True

    def drop_missing(self):
        alive = {id(f) for f in self.app.files}
        gone = [r.id for r in self.rows if id(r.trace) not in alive]
        if gone:
            for rid in gone:
                self._base.pop(rid, None)
                self._spk.pop(rid, None)
            self.rows = [r for r in self.rows if id(r.trace) in alive]
            data_alive = {id(f["data"]) for f in self.app.files}
            self._level_cache = {k: v for k, v in self._level_cache.items() if id(v[0]) in data_alive}
            self._changed(recompute=self.busy)

    def _changed(self, recompute=False):
        if recompute:
            self._start()
        self._refresh()

    # ---------- threshold ----------
    def set_threshold(self, mv):
        try:
            v = float(mv)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(v):
            return False
        self.threshold = v
        self.thr_var.set(f"{v:.4g}")
        self.thr_entry.config(bg=OK_BG)
        self._spk.clear()
        self._start()
        self._refresh()
        return True

    def _on_thr_entry(self):
        if not self.set_threshold(self.thr_var.get().strip()):
            self.thr_entry.config(bg=BAD_BG)

    def _line_px(self):
        if self.thr_line_a is None:
            return None
        return self.ax_a.transData.transform((self.thr_line_a.get_xdata()[0], 1.0))[0]

    def _on_press(self, ev):
        px = self._line_px()
        if ev.button == 1 and ev.inaxes is self.ax_a and px is not None and abs(ev.x - px) <= GRAB_PX:
            self._dragging = True
            self._drag_rel = None

    def _on_motion(self, ev):
        if not self._dragging or ev.x is None:
            return
        self._drag_rel = self.ax_a.transData.inverted().transform((ev.x, ev.y))[0]
        self.thr_line_a.set_xdata([self._drag_rel, self._drag_rel])
        self.canvas.draw_idle()

    def _on_release(self, ev):
        if not self._dragging:
            return
        self._dragging = False
        if ev.x is not None:
            self._drag_rel = self.ax_a.transData.inverted().transform((ev.x, ev.y))[0]
        if self._drag_rel is not None:
            self.set_threshold(self.reference + self._drag_rel)

    # ---------- computing ----------
    def _start(self):
        self._cancel.set()
        self._cancel = cancel = threading.Event()
        self._gen += 1
        gen = self._gen
        jobs = []
        for r in self.rows:
            b = self._base.get(r.id)
            known = None if b is None else (b.get("baseline"), b.get("sigma"), "error" in b)
            jobs.append((r.id, r.trace["data"], r.trace.get("file", ""), r.t_from, r.t_to, known, r.role))
        thr = self.threshold
        self.busy = True
        threading.Thread(target=self._work, args=(gen, cancel, jobs, thr), daemon=True).start()

    def _levels(self, data, i0, i1):
        """Level counts of a window, cached per trace object (the entry keeps the object, so a new
        trace that happens to get a freed trace's id() never reuses its results)."""
        key = (id(data), i0, i1)
        hit = self._level_cache.get(key)
        if hit is not None and hit[0] is data:
            return hit[1], hit[2]
        lv = histogram.level_counts(data, i0, i1)
        self._level_cache[key] = (data, lv[0], lv[1])
        return lv

    def _work(self, gen, cancel, jobs, thr):            # never touches Tk
        put = self._q.put
        try:
            bases = {}
            for k, (rid, data, name, a, b, known, _role) in enumerate(jobs, 1):
                if cancel.is_set():
                    return
                if known is not None:
                    bases[rid] = known
                    continue
                put(("progress", gen, k, len(jobs), name))
                try:
                    i0, i1 = data.index_range(a, b)
                    if i1 - i0 < 2:
                        put(("base", gen, rid, {"error": NO_SAMPLES}))
                        bases[rid] = (None, None, True)
                        continue
                    lv = self._levels(data, i0, i1)
                    base, sigma = histogram.robust_baseline(*lv)
                    dur = max(min(b, data.t_end) - max(a, data.t_start), 0.0)
                except Exception as e:                   # this row only; the others continue
                    put(("base", gen, rid, {"error": f"error: {e}"}))
                    bases[rid] = (None, None, True)
                    continue
                put(("base", gen, rid, {"baseline": base, "sigma": sigma, "levels": lv[0], "counts": lv[1],
                                        "duration": dur}))
                bases[rid] = (base, sigma, False)
            if thr is None:
                thr = default_threshold([(bases[j[0]], j[6]) for j in jobs if j[0] in bases])
                put(("threshold", gen, thr))
            for k, (rid, data, name, a, b, _, _role) in enumerate(jobs, 1):
                if cancel.is_set():
                    return
                if bases.get(rid, (None, None, True))[2]:
                    continue
                try:
                    put(("spikes", gen, rid, noise.find_spikes_at(data, a, b, thr)))
                except Exception as e:                   # this row only
                    put(("base", gen, rid, {"error": f"error: {e}"}))
        except Exception as e:                           # shown in the status line
            put(("error", gen, repr(e)))
        finally:
            put(("done", gen))

    def _poll(self):
        if self._closed:
            return
        try:
            self._poll_once()
        finally:                                         # keep polling even if a redraw fails
            if not self._closed:
                self._after_id = self.after(50, self._poll)

    def _poll_once(self):
        changed = done = False
        try:
            while True:
                msg = self._q.get_nowait()
                kind, gen = msg[0], msg[1]
                if gen != self._gen:
                    continue
                ids = {r.id for r in self.rows}
                if kind == "progress":
                    self.status_lbl.config(text=f"Computing {msg[2]}/{msg[3]} · {msg[4]}")
                elif kind == "base" and msg[2] in ids:
                    self._base[msg[2]] = msg[3]
                    changed = True
                elif kind == "threshold":
                    self.threshold = msg[2]
                    self.thr_var.set(f"{msg[2]:.4g}")
                elif kind == "spikes" and msg[2] in ids:
                    self._spk[msg[2]] = msg[3]
                    changed = True
                elif kind == "error":
                    self.status_lbl.config(text=f"Couldn't compute: {msg[2]}")
                elif kind == "done":
                    done = True
        except queue.Empty:
            pass
        if done:
            self.busy = False
            if not str(self.status_lbl.cget("text")).startswith("Couldn't"):
                self.status_lbl.config(text="")
            self._refresh()
        elif changed:
            self._refresh_table()

    # ---------- results ----------
    def _gain(self, row):
        return run_info.effective_gain(self._info(row.trace), row.trace.get("meta", {}))

    def _run(self, row):
        return self._run_number(row.trace)

    def _sorted_rows(self):
        def key(r):
            g = self._gain(r)[0]
            run = self._run(r)
            return (g is None, -(g or 0), run if run is not None else 10 ** 9, r.t_from)
        return sorted(self.rows, key=key)

    def _stats(self):
        stats = []
        for r in self.rows:
            b, sp = self._base.get(r.id), self._spk.get(r.id)
            if b is None or "error" in b or sp is None:
                continue
            stats.append(histogram.RowStat(r.id, self._run(r), self._gain(r)[0], r.role, r.t_from, r.t_to,
                                           len(sp.times), b["duration"], run_info.file_key(r.trace["path"])))
        return stats

    def _update_reference(self):
        rows = [(self._base[r.id]["baseline"], r.role) for r in self.rows
                if r.id in self._base and "error" not in self._base[r.id]]
        self.reference = histogram.reference_level([b for b, _ in rows], [x for _, x in rows])

    def table(self):
        stats = {s.row_id: s for s in self._stats()}
        comp, _ = histogram.compare(list(stats.values()))
        out = []
        for r in self._sorted_rows():
            g, src = self._gain(r)
            gain_txt = DASH if g is None else (f"{g:g}" if src == "override" else f"{g:g} (file)")
            b, sp, st = self._base.get(r.id), self._spk.get(r.id), stats.get(r.id)
            cells = {"Plot": "yes" if r.plot else "", "Run": self._run(r), "Sample": ROLE_TEXT[r.role],
                     "Gain (mV)": gain_txt, "From (s)": r.t_from, "To (s)": r.t_to}
            if b is not None and "error" in b:
                cells.update({"Baseline (mV)": DASH, "Spikes": b["error"]})
            else:
                cells["Baseline (mV)"] = b["baseline"] if b else DASH
                cells["Spikes"] = len(sp.times) if sp is not None else DASH
            if st is not None:
                hts = sp.heights + sp.baseline - b["baseline"]     # peaks above the row's robust baseline
                c = comp[r.id]
                cells.update({"Rate (/s)": st.rate, "±": st.err,
                              "Median peak (mV)": float(np.median(hts)) if len(hts) else DASH,
                              "Max peak (mV)": float(np.max(hts)) if len(hts) else DASH,
                              "× negative": DASH if c.x_negative is None else c.x_negative,
                              "SD above negative": DASH if c.sd_above is None else c.sd_above,
                              "Above limit": DASH if c.above_limit is None else ("yes" if c.above_limit else "no")})
            out.append([cells.get(col, DASH) for col in COLUMNS])
        return pd.DataFrame(out, columns=COLUMNS)

    def summary_lines(self):
        return histogram.compare(self._stats())[1]

    def plotted_count(self):
        return len(self._a_artists), len(self._b_artists)

    def b_message(self):
        return self._b_msg

    def _refresh(self):
        self._update_reference()
        if self.threshold is not None:
            self.ref_lbl.config(text=f"= reference {self.reference:.2f} + {self.threshold - self.reference:.2f} mV")
        self._refresh_table()
        self.summary_lbl.config(text="\n".join(self.summary_lines()))
        self._draw()

    def _refresh_table(self):
        df = self.table()
        sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for r, (_, vals) in zip(self._sorted_rows(), df.iterrows()):
            self.tree.insert("", tk.END, iid=str(r.id),
                             values=[v if isinstance(v, str) or v is None else self._fmt(v) for v in vals])
        self.tree.selection_set([i for i in sel if self.tree.exists(i)])

    def _draw(self):
        ax, bx = self.ax_a, self.ax_b
        ax.clear(); bx.clear()
        self._a_artists, self._b_artists = [], []
        self.thr_line_a = None
        ref = self.reference
        rows = [r for r in self.rows if r.plot and r.id in self._base and "error" not in self._base[r.id]]
        if rows:
            sets = [self._base[r.id]["levels"] for r in rows]
            top = max(float(s.max()) for s in sets)
            bottom = min(float(s.min()) for s in sets)
            edges = histogram.sample_edges(sets, ref, top, bottom)
            for r in rows:
                b = self._base[r.id]
                d = histogram.sample_density(b["levels"], b["counts"], edges, ref, b["duration"])
                d = np.where(d > 0, d, np.nan)
                self._a_artists.append(ax.stairs(d, edges, color=r.trace.get("color") or "#0f766e",
                                                 ls="--" if r.dashed else "-", lw=1.3))
            ax.set_xscale("symlog", linthresh=histogram.LIN_MV)
            ax.set_yscale("log")
        ax.set_title("A. Every sample", loc="left", fontsize=10)
        ax.set_xlabel(f"signal − {ref:.2f} mV (linear to ±{histogram.LIN_MV:g}, log beyond)")
        ax.set_ylabel("samples per s per mV")
        self._b_msg = ""
        if self.threshold is not None:
            rel = self.threshold - ref
            self.thr_line_a = ax.axvline(rel, color=THR_COLOR, ls="--", lw=1.2)
            peaks = [(r, self._spk[r.id]) for r in rows if r.id in self._spk]
            top_b = max([float(np.max(sp.heights + sp.baseline - ref)) for _, sp in peaks if len(sp.heights)]
                        + [rel * 10])
            eb = histogram.spike_edges(rel, top_b)
            if eb is None:
                self._b_msg = f"Set the threshold above the reference level ({ref:.2f} mV)"
                bx.text(0.5, 0.5, self._b_msg, ha="center", va="center", transform=bx.transAxes, color=MUTED)
            else:
                for r, sp in peaks:
                    dur = self._base[r.id]["duration"]
                    hist = np.histogram(sp.heights + sp.baseline - ref, eb)[0] / max(dur, 1e-12)
                    hist = np.where(hist > 0, hist, np.nan)
                    self._b_artists.append(bx.stairs(hist, eb, color=r.trace.get("color") or "#0f766e",
                                                     ls="--" if r.dashed else "-", lw=1.3))
                bx.set_xscale("log")
                if self._b_artists:
                    bx.set_yscale("log")
                bx.axvline(rel, color=THR_COLOR, ls="--", lw=1.2)
        bx.set_title("B. Spike peaks", loc="left", fontsize=10)
        bx.set_xlabel(f"spike peak − {ref:.2f} mV")
        bx.set_ylabel("spikes per s per bin")
        try:
            self.fig.tight_layout()
        except Exception:
            pass
        self.canvas.draw_idle()

    # ---------- table interaction ----------
    def _selected_ids(self):
        return [int(i) for i in self.tree.selection()]

    def _on_duplicate(self):
        for rid in self._selected_ids():
            self.duplicate_row(rid)

    def _on_remove(self):
        for rid in self._selected_ids():
            self.remove_row(rid)

    def _on_tree_click(self, ev):
        if self.tree.identify_region(ev.x, ev.y) != "cell":
            return
        col = self.tree.identify_column(ev.x)
        iid = self.tree.identify_row(ev.y)
        if iid and col == "#1":                      # Plot tick
            row = self._row(int(iid))
            if not self.set_row_plot(row.id, not row.plot):
                self.status_lbl.config(text=f"At most {histogram.MAX_PLOTTED} rows are plotted.")

    # ---------- run info editing ----------
    def _save_store(self):
        try:
            run_info.save(self.app.run_info_path, self.store)
        except OSError as e:
            messagebox.showerror("Run info", f"Couldn't save the run info: {e}", parent=self)

    def edit_gain(self, row_id, text):
        """Set the PMT gain of the row's file (saved as run info); empty text = use the file header."""
        s = str(text).strip()
        if s:
            try:
                g = float(s)
            except ValueError:
                return False
            if not math.isfinite(g):
                return False
        row = self._row(row_id)
        key = run_info.file_key(row.trace["path"])
        info = self.store.get(key) or run_info.RunInfo()
        self.store[key] = run_info.RunInfo(info.role, info.label, float(s) if s else None)
        self._save_store()
        self._refresh()
        return True

    def edit_sample(self, row_id, role, label=None):
        """Role of this row only (use save_row_as_run_info to keep it for the file)."""
        row = self._row(row_id)
        if label is not None:
            row.label = str(label)
        self.set_row_role(row_id, role)

    def save_row_as_run_info(self, row_id):
        row = self._row(row_id)
        key = run_info.file_key(row.trace["path"])
        info = self.store.get(key) or run_info.RunInfo()
        self.store[key] = run_info.RunInfo("" if row.role == "none" else row.role, row.label, info.gain_mv)
        self._save_store()

    def _store_changed(self, keys):
        """Run info of these files changed (import): their rows take the new role."""
        self._save_store()
        for r in self.rows:
            if run_info.file_key(r.trace["path"]) in keys and not r.role_manual:
                info = self.store[run_info.file_key(r.trace["path"])]
                r.role, r.label = (info.role or "none"), info.label
        self._refresh()

    def open_import(self, path=None):
        if path is None:
            path = filedialog.askopenfilename(parent=self, title="Import run info",
                                              filetypes=[("Excel workbook", "*.xlsx *.xlsm"), ("All files", "*.*")])
            if not path:
                return None
        try:
            sheets = run_info.read_workbook(path)
        except run_info.ImportFailed as e:
            messagebox.showerror("Import run info", str(e), parent=self)
            return None
        return ImportDialog(self, path, sheets)

    def _cell(self, ev):
        iid, col = self.tree.identify_row(ev.y), self.tree.identify_column(ev.x)
        if not iid:
            return None, None
        return int(iid), COLUMNS[int(col[1:]) - 1] if col.startswith("#") and col[1:].isdigit() else None

    def _on_tree_double(self, ev):
        rid, col = self._cell(ev)
        if rid is None:
            return
        row = self._row(rid)
        if col == "Gain (mV)":
            g = self._gain(row)
            self._inline_entry(ev, "" if g[1] != "override" else f"{g[0]:g}", lambda t: self.edit_gain(rid, t))
        elif col == "Sample":
            self._sample_popup(ev, row)
        elif col in ("From (s)", "To (s)"):
            cur = row.t_from if col == "From (s)" else row.t_to
            self._inline_entry(ev, f"{cur:g}", lambda t: self.set_row_window(
                rid, t if col == "From (s)" else row.t_from, t if col == "To (s)" else row.t_to))

    def _inline_entry(self, ev, text, commit):
        x, y, w, h = self.tree.bbox(self.tree.identify_row(ev.y), self.tree.identify_column(ev.x))
        e = tk.Entry(self.tree, relief=tk.SOLID, bd=1)
        e.insert(0, text)
        e.place(x=x, y=y, width=w, height=h)
        e.focus_set()

        def done(save):
            if save and not commit(e.get()):
                e.config(bg=BAD_BG)
                return
            e.destroy()
        e.bind("<Return>", lambda _: done(True))
        e.bind("<Escape>", lambda _: done(False))
        e.bind("<FocusOut>", lambda _: e.destroy())

    def _sample_popup(self, ev, row):
        menu = tk.Menu(self, tearoff=False)
        for role, text in ROLE_TEXT.items():
            menu.add_command(label=text, command=lambda r=role: self.edit_sample(row.id, r))
        menu.add_separator()
        menu.add_command(label="Save as run info", command=lambda: self.save_row_as_run_info(row.id))
        menu.tk_popup(ev.x_root, ev.y_root)

    def _on_tree_menu(self, ev):
        rid, _ = self._cell(ev)
        if rid is not None:
            self._sample_popup(ev, self._row(rid))

    # ---------- export ----------
    def _export_tail(self):
        lines = [[s] for s in self.summary_lines()]
        lines += [[], ["Threshold (mV)", self.threshold], ["Reference (mV)", self.reference]]
        for r in self._sorted_rows():
            lines.append(["File", r.trace["path"], "Gain source", self._gain(r)[1] or "none"])
        return lines

    def export(self, path):
        df = self.table()
        if path.lower().endswith((".xlsx", ".xlsm")):
            with pd.ExcelWriter(path) as w:
                df.to_excel(w, sheet_name="Histogram", index=False)
                pd.DataFrame(self._export_tail()).to_excel(w, sheet_name="Summary", index=False, header=False)
            return
        import csv
        with open(path, "w", encoding="utf-8", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(COLUMNS)
            wr.writerows(df.itertuples(index=False))
            wr.writerow([])
            wr.writerows(self._export_tail())

    def save_image(self, path):
        self.fig.savefig(path, dpi=150, facecolor=CARD)

    def _on_export(self):
        path = filedialog.asksaveasfilename(parent=self, title="Export histogram results", defaultextension=".csv",
                                            initialfile="histogram_results.csv",
                                            filetypes=[("CSV", "*.csv"), ("Excel workbook", "*.xlsx")])
        if path:
            try:
                self.export(path)
            except OSError as e:
                messagebox.showerror("Export", f"Couldn't write the file: {e}", parent=self)

    def _on_save_image(self):
        path = filedialog.asksaveasfilename(parent=self, title="Save histogram image", defaultextension=".png",
                                            initialfile="histogram.png", filetypes=[("PNG image", "*.png")])
        if path:
            self.save_image(path)

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
        if getattr(self.app, "_histogram", None) is self:
            self.app._histogram = None
        self.destroy()


NONE = "(none)"


class ImportDialog(tk.Toplevel):
    """Choose the sheet and columns of a summary workbook, preview the run info, apply it."""
    PCOLS = ("✓", "Run", "File", "Gain (mV)", "Label", "Role", "Status")

    def __init__(self, window, path, sheets):
        super().__init__(window)
        self.window, self.path, self.sheets = window, path, sheets
        self.title(f"Import run info · {os.path.basename(path)}")
        self.geometry("980x520")
        self.configure(background=CARD)
        self.choice = run_info.guess_choice(sheets)
        self.preview = []
        self._build()
        self._load_choice()
        self._update_preview()

    def _combo(self, parent, label, values, col):
        ttk.Label(parent, text=label, style="Card.TLabel").grid(row=0, column=col, sticky="w", padx=(0, 4))
        var = tk.StringVar(master=self)
        cb = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=16)
        cb.grid(row=1, column=col, sticky="w", padx=(0, 10))
        cb.bind("<<ComboboxSelected>>", lambda ev: self._on_change())
        return var, cb

    def _build(self):
        top = ttk.Frame(self, style="Card.TFrame", padding=(12, 10))
        top.pack(fill=tk.X)
        self.sheet_var, _ = self._combo(top, "Sheet", list(self.sheets), 0)
        self.run_var, self.run_cb = self._combo(top, "Run column", [], 1)
        self.gain_var, self.gain_cb = self._combo(top, "Gain column", [], 2)
        self.sample_var, self.sample_cb = self._combo(top, "Sample column", [], 3)
        self.lk_sheet_var, _ = self._combo(top, "Lookup sheet", [NONE] + list(self.sheets), 4)
        self.lk_key_var, self.lk_key_cb = self._combo(top, "Lookup key", [], 5)
        self.lk_label_var, self.lk_label_cb = self._combo(top, "Lookup label", [], 6)
        mid = ttk.Frame(self, style="Card.TFrame", padding=(12, 0))
        mid.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(mid, columns=self.PCOLS, show="headings", selectmode="none")
        for c in self.PCOLS:
            self.tree.heading(c, text=c, anchor="w")
            self.tree.column(c, width=40 if c == "✓" else 300 if c == "File" else 110, anchor="w")
        ys = ttk.Scrollbar(mid, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ys.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.bind("<Button-1>", self._on_click)
        bot = ttk.Frame(self, style="Card.TFrame", padding=(12, 8))
        bot.pack(fill=tk.X)
        ttk.Label(bot, text="Rows matched by run # among the loaded traces in this workbook's folder. "
                            "Click ✓ to include or leave out a row.", style="Muted.Card.TLabel").pack(side=tk.LEFT)
        ttk.Button(bot, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        self.apply_btn = ttk.Button(bot, text="Apply", command=self.apply)
        self.apply_btn.pack(side=tk.RIGHT, padx=6)

    def _cols(self, sheet):
        return [str(c) for c in self.sheets[sheet].columns] if sheet in self.sheets else []

    def _load_choice(self):
        c = self.choice
        self.sheet_var.set(c.sheet)
        cols = self._cols(c.sheet)
        for cb in (self.run_cb, self.gain_cb, self.sample_cb):
            cb.configure(values=([NONE] if cb is not self.run_cb else []) + cols)
        self.run_var.set(c.run_col)
        self.gain_var.set(c.gain_col or NONE)
        self.sample_var.set(c.sample_col or NONE)
        if c.lookup:
            self.lk_sheet_var.set(c.lookup[0])
            lk = self._cols(c.lookup[0])
            self.lk_key_cb.configure(values=lk)
            self.lk_label_cb.configure(values=lk)
            self.lk_key_var.set(c.lookup[1])
            self.lk_label_var.set(c.lookup[2])
        else:
            self.lk_sheet_var.set(NONE)

    def _on_change(self):
        sheet = self.sheet_var.get()
        if sheet != self.choice.sheet:
            self.choice = run_info.guess_choice({sheet: self.sheets[sheet],
                                                 **{k: v for k, v in self.sheets.items() if k != sheet}})
            self._load_choice()
        else:
            lk_sheet = self.lk_sheet_var.get()
            lk = None
            if lk_sheet != NONE:
                cols = self._cols(lk_sheet)
                self.lk_key_cb.configure(values=cols)
                self.lk_label_cb.configure(values=cols)
                key = self.lk_key_var.get() if self.lk_key_var.get() in cols else (cols[0] if cols else "")
                lab = self.lk_label_var.get() if self.lk_label_var.get() in cols else (cols[1] if len(cols) > 1 else key)
                self.lk_key_var.set(key); self.lk_label_var.set(lab)
                lk = (lk_sheet, key, lab) if key else None
            none = lambda v: None if v == NONE else v
            self.choice = run_info.ImportChoice(sheet, self.run_var.get(), none(self.gain_var.get()),
                                                none(self.sample_var.get()), lk)
        self._update_preview()

    def _update_preview(self):
        traces = [(f["path"], self.window._run_number(f)) for f in self.window.app.files]
        try:
            self.preview = run_info.build_preview(self.sheets, self.choice, self.path, traces)
        except KeyError:
            self.preview = []
        self._render()

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(self.preview):
            self.tree.insert("", tk.END, iid=str(i), values=(
                "✓" if p.ticked else "", p.run, os.path.basename(p.path) if p.path else DASH,
                DASH if p.gain is None else f"{p.gain:g}", p.label, p.role or DASH, p.status))
        self.apply_btn.state(["!disabled" if any(p.ticked for p in self.preview) else "disabled"])

    def set_ticked(self, i, on):
        p = self.preview[i]
        p.ticked = bool(on) and p.path is not None and p.status == "ok"     # only matched, valid rows
        self._render()

    def _on_click(self, ev):
        iid = self.tree.identify_row(ev.y)
        if iid and self.tree.identify_column(ev.x) == "#1":
            i = int(iid)
            self.set_ticked(i, not self.preview[i].ticked)

    def apply(self):
        n = run_info.apply_preview(self.window.store, self.preview)
        self.window._store_changed({run_info.file_key(p.path) for p in self.preview if p.ticked and p.path})
        self.destroy()
        return n
