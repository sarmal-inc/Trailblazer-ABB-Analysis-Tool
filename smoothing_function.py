"""Rolling-average smoothing tab for the analysis tools.

Usage from the main app:

    from smoothing import open_smoothing_tab
    open_smoothing_tab(self.notebook, x, y, name="Trace 1",
                       x_label="Time (s)", y_label="Voltage (V)")

The tab lets the user pick a window size (number of points to average over)
and a start x value. Points before the start x are left as they are; points
from the start x onward are replaced by a rolling average computed with
pandas (centered, min_periods=1, so there are no NaNs or edge droop).

Run this file directly to try the tab on demo data.
"""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
import pandas as pd

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    HAS_MPL = True
except Exception:  # chart is optional
    HAS_MPL = False

MAX_PLOT_POINTS = 200_000  # decimate the display (not the data) above this


def rolling_average(x, y, window, start_x=None, center=True):
    """Smooth y with a rolling mean, starting at the first point where x >= start_x.

    Returns (smoothed, start_index). `smoothed` has the same length as y and is
    NaN before start_index. Averaging uses only points from start_index on, so
    data before the start never leaks into the result. Points are taken in
    sample order, which is what you want for time traces.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if x.shape != y.shape:
        raise ValueError("x and y must be the same length")
    window = int(window)
    if window < 1:
        raise ValueError("window must be at least 1")

    if start_x is None:
        start = 0
    else:
        hits = np.flatnonzero(x >= start_x)
        start = int(hits[0]) if hits.size else len(y)

    smoothed = np.full(y.shape, np.nan)
    if start < len(y):
        seg = pd.Series(y[start:])
        smoothed[start:] = seg.rolling(window, center=center, min_periods=1).mean().to_numpy()
    return smoothed, start


def _fmt(v):
    return f"{v:.6g}"


class SmoothingTab(ttk.Frame):
    """A notebook tab that previews and exports a rolling-average transform."""

    def __init__(self, notebook, x, y, name="trace", x_label="x", y_label="y",
                 window=5, start_x=None, on_apply=None):
        super().__init__(notebook)
        self.notebook = notebook
        self.x = np.asarray(x, dtype=float)
        self.y = np.asarray(y, dtype=float)
        self.name = name
        self.x_label = x_label
        self.y_label = y_label
        self.on_apply = on_apply  # optional callback(x, smoothed, params)
        self.smoothed = None
        self.params = {}

        finite_x = self.x[np.isfinite(self.x)]
        default_start = start_x if start_x is not None else (finite_x.min() if finite_x.size else 0.0)
        self.window_var = tk.StringVar(value=str(window))
        self.start_var = tk.StringVar(value=_fmt(default_start))
        self.center_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar()

        self._build()
        self.update_preview()

    # ---------------------------------------------------------------- UI --
    def _build(self):
        ctl = ttk.Frame(self, padding=(8, 8, 8, 2))
        ctl.pack(fill="x")

        ttk.Label(ctl, text="Window (points):").pack(side="left")
        win = ttk.Spinbox(ctl, from_=1, to=max(1, len(self.y)), width=8,
                          textvariable=self.window_var, command=self.update_preview)
        win.pack(side="left", padx=(4, 12))

        ttk.Label(ctl, text="Start x:").pack(side="left")
        start = ttk.Entry(ctl, width=14, textvariable=self.start_var)
        start.pack(side="left", padx=(4, 12))

        ttk.Checkbutton(ctl, text="Centered", variable=self.center_var,
                        command=self.update_preview).pack(side="left", padx=(0, 12))
        ttk.Button(ctl, text="Apply", command=self.update_preview).pack(side="left")

        for w in (win, start):
            w.bind("<Return>", lambda e: self.update_preview())

        ttk.Button(ctl, text="Close tab", command=self.close).pack(side="right")
        ttk.Button(ctl, text="Export CSV…", command=self.export_csv).pack(side="right", padx=4)
        if self.on_apply is not None:
            ttk.Button(ctl, text="Send to main chart", command=self._send).pack(side="right")

        ttk.Label(self, textvariable=self.status_var, padding=(8, 0, 8, 4),
                  foreground="#555").pack(fill="x")

        if HAS_MPL:
            self.fig = Figure(figsize=(8, 4.5), dpi=100)
            self.ax = self.fig.add_subplot(111)
            self.canvas = FigureCanvasTkAgg(self.fig, master=self)
            bar = ttk.Frame(self)
            bar.pack(side="bottom", fill="x")
            self.toolbar = NavigationToolbar2Tk(self.canvas, bar)
            self.toolbar.update()
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
            self.canvas.mpl_connect("button_press_event", self._on_click)
        else:
            ttk.Label(self, padding=20,
                      text="matplotlib is not installed, so there is no preview chart.\n"
                           "Smoothing and CSV export still work.").pack()

    # ------------------------------------------------------------- logic --
    def _read_inputs(self):
        try:
            window = int(float(self.window_var.get()))
        except ValueError:
            raise ValueError("Window must be a whole number.")
        if window < 1:
            raise ValueError("Window must be at least 1.")
        text = self.start_var.get().strip()
        try:
            start_x = float(text) if text else None
        except ValueError:
            raise ValueError("Start x must be a number (or blank for the first point).")
        return window, start_x

    def update_preview(self):
        try:
            window, start_x = self._read_inputs()
        except ValueError as e:
            self.status_var.set(str(e))
            return

        center = self.center_var.get()
        self.smoothed, i0 = rolling_average(self.x, self.y, window, start_x, center)
        self.params = {"window": window, "start_x": start_x, "center": center}

        n = len(self.y)
        n_smooth = n - i0
        if n_smooth == 0:
            self.status_var.set("Start x is past the last point, so nothing was smoothed.")
        else:
            resid = self.y[i0:] - self.smoothed[i0:]
            msg = (f"{n_smooth:,} of {n:,} points smoothed with a {window}-point "
                   f"{'centered' if center else 'trailing'} average.  "
                   f"Std of removed noise: {_fmt(np.nanstd(resid))}")
            if window > n_smooth:
                msg += "  (Window is larger than the smoothed range.)"
            if HAS_MPL:
                msg += "   Tip: click the chart to set Start x."
            self.status_var.set(msg)

        if HAS_MPL:
            self._draw(start_x, i0)

    def _draw(self, start_x, i0):
        step = max(1, len(self.y) // MAX_PLOT_POINTS)
        xs, ys, ss = self.x[::step], self.y[::step], self.smoothed[::step]

        ax = self.ax
        ax.clear()
        ax.plot(xs, ys, lw=0.8, alpha=0.45, color="tab:gray", label="Raw")
        ax.plot(xs, ss, lw=1.8, color="tab:blue",
                label=f"Rolling avg ({self.params['window']} pts)")
        if i0 < len(self.x):
            ax.axvline(self.x[i0], ls="--", lw=1, color="tab:red", label="Start x")
        ax.set_xlabel(self.x_label)
        ax.set_ylabel(self.y_label)
        title = f"Smoothing: {self.name}"
        if step > 1:
            title += f"  (display shows every {step}th point)"
        ax.set_title(title)
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best")
        self.fig.tight_layout()
        self.canvas.draw_idle()

    def _on_click(self, event):
        # Ignore clicks while the zoom/pan tools are active.
        if self.toolbar.mode or event.button != 1 or event.inaxes is not self.ax:
            return
        if event.xdata is None:
            return
        self.start_var.set(_fmt(event.xdata))
        self.update_preview()

    # ----------------------------------------------------------- actions --
    def export_csv(self):
        if self.smoothed is None:
            return
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.name)
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv", initialfile=f"{safe}_smoothed.csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        df = pd.DataFrame({self.x_label: self.x, self.y_label: self.y,
                           f"{self.y_label} (smoothed)": self.smoothed})
        try:
            df.to_csv(path, index=False)
        except OSError as e:
            messagebox.showerror("Export failed", str(e), parent=self)
            return
        self.status_var.set(f"Exported to {path}")

    def _send(self):
        if self.smoothed is not None:
            self.on_apply(self.x, self.smoothed, dict(self.params))

    def close(self):
        self.notebook.forget(self)
        self.destroy()


def open_smoothing_tab(notebook, x, y, name="trace", **kwargs):
    """Add a SmoothingTab to `notebook`, switch to it, and return it."""
    tab = SmoothingTab(notebook, x, y, name=name, **kwargs)
    label = f"Smooth: {name}"
    notebook.add(tab, text=label if len(label) <= 30 else label[:29] + "…")
    notebook.select(tab)
    return tab


# ------------------------------------------------------------------ demo --
if __name__ == "__main__":
    rng = np.random.default_rng(0)
    t = np.linspace(0, 10, 2000)
    sig = np.sin(t) + 0.3 * rng.standard_normal(t.size)

    root = tk.Tk()
    root.title("Smoothing demo")
    root.geometry("1000x650")
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)

    main = ttk.Frame(nb, padding=20)
    nb.add(main, text="Main")
    ttk.Button(main, text="Smooth…",
               command=lambda: open_smoothing_tab(
                   nb, t, sig, name="Noisy sine", x_label="Time (s)", y_label="Signal",
                   window=25, on_apply=lambda x, s, p: print("Applied", p))
               ).pack(anchor="w")
    root.mainloop()
