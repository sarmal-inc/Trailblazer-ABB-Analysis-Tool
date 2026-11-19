#!/usr/bin/env python3
"""
Multi-File Analyzer
-------------------
Load one or more BB DAQ run CSV files, see the data points on one plot
(superimposed), drag or type a time window, and get max, min, mean and
standard deviation of mV for every file inside that window.

Expected CSV layout (csv_simulator.py writes the same thing):

    # BB DAQ run 1 · experiment "Test pars data" · started 2026-10-16 08:25:50
    # sample_rate_hz 50000
    # led 2 at 100%
    # pmt_gain_mv 800
    # concentration_nm 15
    # position_mm -33 -56 5e-06
    # recipe "..." step 7 channel CH12          (optional)
    time_s,value_mV
    0.00000,-0.9375
    ...
    # samples 250880
    # duration_s 5.01758
    # max_mv / min_mv / mean_mv / std_mv ...
    # stopped_early no

Excel workbooks (.xlsx / .xlsm) exported by the instrument are read too:

    Data      Time (s) | Channel 1 raw signal (mV) | Channel 2 raw signal (mV) | ...
              (column "Channel N" is run N; columns may be different lengths)
    Metadata  one row per run: Experiment name, Run #, Date collected,
              Sample rate (Hz), PMT gain (mV), LED channel, X, Y, Z, ...
    Readouts  one row per run: Max (mV), Min (mV), Average (mV), Std dev (mV), ...

Every signal column becomes its own trace, as if it were a separate CSV file.
A workbook with only a data sheet (time in the first column) also works.

Large files (millions of rows) are fine: statistics always use every point;
the plot shows every point when zoomed in, and the min and max of each small
slice when zoomed out (so spikes are never hidden).

Cache:
    The first time a file is opened it is converted (about 1 to 3 s for a 7 M-sample run) into a
    compact binary form in %LOCALAPPDATA%\\BBAnalyzer\\cache, about 35 MB per run (about 5.6 GB
    for 160 runs). Opening it again, now or in a later session, is near-instant. A CSV that was
    edited or re-exported gets a new entry; the old one stays, because nothing is evicted
    automatically. To free the space use File > Cache... > Clear cache (entries of traces that are
    still loaded are kept: use Clear all first), or delete the folder while the app is closed.
    The cache is only ever a copy: deleting it loses nothing, files are converted again when
    needed. trace_store.py must sit next to this file.

Noise analysis:
    Analyze noise (toolbar) measures the shown traces (all of them when "Shown files only" is
    unticked, as Calculate does) over the current window using the Noise settings (spike
    threshold k, minimum frequency, watched frequencies) and fills the Noise tab: spike count
    and rate, the strongest spectrum peaks and the amplitude at each watched frequency.
    Noise view... opens one trace in detail (the spike list and the spectrum): use
    View > Noise view... or double-click a row of the Noise tab. Noise settings... edits the
    parameters; they are saved in
    %LOCALAPPDATA%\\BBAnalyzer\\settings.json and restored at the next start.

Chart tools:
    The toolbar under the plots switches the mouse mode: Select window (W), Zoom ↔ (X),
    Zoom ↕ (Y) and Cursors (C). Back (BackSpace) returns to the previous view, Reset zoom
    (Home) to the full range, and Save image... writes the plot to a PNG file. The keys
    W, X, Y, C, BackSpace and Home work only after the chart has focus: click it first.

Smoothing:
    Smooth... (toolbar) or View > Smooth selected trace... opens a tab next to Statistics and
    Noise for the selected trace (or the first shown one). Pick a window size in points and a
    start x (defaults to the window start; click the chart to set it). Points from start x on are
    replaced by a pandas rolling mean. Export CSV... saves raw and smoothed columns; Send to main
    chart adds the result as a new trace "<name> (smoothed N pts)" (held in memory, not cached),
    which then works with Calculate, Analyze noise and exports like any other trace.
    smoothing_function.py must sit next to this file.

Requirements:
    pip install pandas matplotlib openpyxl numpy
    pip install tkinterdnd2      (optional: drag files or folders onto the window)
    trace_store.py in the same folder as this file

Run:
    python multi_file_analyzer.py
"""

import dataclasses
import math
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont

import numpy as np
import pandas as pd

import noise
from settings import default_settings_path, load_settings, parse_settings, save_settings
from trace_store import (TraceData, cache_info, clear_cache, default_cache_dir, is_cached,
                         is_cloud_only, open_traces)
from smoothing_function import open_smoothing_tab

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from chart_tools import ChartTools
    HAS_MPL = True
except Exception:  # chart is optional
    HAS_MPL = False

try:  # drag and drop from Explorer is optional:  pip install tkinterdnd2
    from tkinterdnd2 import TkinterDnD, DND_FILES
    TkBase = TkinterDnD.Tk
    HAS_DND = True
except Exception:
    TkBase = tk.Tk
    HAS_DND = False


# --------------------------------------------------------------------------
# Core logic (no GUI), easy to reuse or test on its own
# --------------------------------------------------------------------------
class ReadError(Exception):
    pass


TITLE_RE = re.compile(r'run\s+(\d+).*?experiment\s+"([^"]*)"(?:.*?started\s+(.+))?', re.I)
RECIPE_RE = re.compile(r'^"?([^"]*)"?\s*(?:step\s+(\S+))?\s*(?:channel\s+(\S+))?', re.I)


def _parse_comment(line, dest):
    """'# key value...' -> dest[key] = value. Handles the 'BB DAQ run' title and recipe lines."""
    s = line.lstrip("#").strip()
    if not s:
        return
    m = TITLE_RE.search(s)
    if m and s.lower().startswith(("bb daq", "run")):
        dest["title"] = s
        dest["run"] = m.group(1)
        dest["experiment"] = m.group(2)
        if m.group(3):
            dest["started"] = m.group(3).strip()
        return
    parts = s.split(None, 1)
    key, val = parts[0], (parts[1].strip() if len(parts) > 1 else "")
    dest[key] = val
    if key.lower() == "recipe":
        r = RECIPE_RE.match(val)
        if r:
            dest["recipe_name"] = r.group(1).strip()
            if r.group(2):
                dest["step"] = r.group(2)
            if r.group(3):
                dest["channel"] = r.group(3)


def _footer_lines(path, nbytes=8192):
    """Comment lines at the very end of the file, read without loading the whole file."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        fh.seek(max(0, size - nbytes))
        tail = fh.read().decode("utf-8", errors="replace").splitlines()
    out = []
    for ln in reversed(tail):
        if not ln.strip():
            continue
        if not ln.lstrip().startswith("#"):
            break
        out.append(ln)
    return list(reversed(out))


def parse_instrument_csv(path):
    """Return dict(path, file, name, meta, summary, t_label, v_label, t, mv)."""
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except PermissionError:
        raise ReadError("The file is locked. Close it in Excel (or any other program) and try again.")
    except OSError as e:
        raise ReadError(f"The file couldn't be opened: {e}")

    meta, header, skip = {}, None, 0
    with fh:
        for i, ln in enumerate(fh):
            if i > 200:
                break
            s = ln.strip()
            if not s:
                continue
            if s.startswith("#"):
                _parse_comment(s, meta)
                continue
            header, skip = s, i
            break
    if header is None:
        raise ReadError("Couldn't find the header row (e.g. 'time_s,value_mV').")

    try:
        df = pd.read_csv(path, skiprows=skip, header=0, comment="#", usecols=[0, 1],
                         skip_blank_lines=True, encoding="utf-8", encoding_errors="replace",
                         engine="c", dtype="float64")
    except ValueError as e:
        raise ReadError(f"The data rows aren't all numbers ({e}).")
    except Exception as e:
        raise ReadError(f"Couldn't read the data rows: {e}")
    df = df.dropna()
    if df.empty:
        raise ReadError("No numeric data rows were found under the header row.")

    t = df.iloc[:, 0].to_numpy()
    mv = df.iloc[:, 1].to_numpy()
    if np.any(np.diff(t) < 0):              # time must be sorted for fast windowing
        order = np.argsort(t, kind="stable")
        t, mv = t[order], mv[order]

    summary = {}
    for ln in _footer_lines(path):
        _parse_comment(ln, summary)

    cols = [c.strip() for c in df.columns]
    file = os.path.basename(path)
    return {
        "path": path,
        "file": file,
        "folder": os.path.basename(os.path.dirname(path)),
        "name": f"run {meta['run']}" if "run" in meta else os.path.splitext(file)[0],
        "meta": meta,
        "summary": summary,
        "t_label": _pretty_label(cols[0], "Time (s)"),
        "v_label": _pretty_label(cols[1], "Signal (mV)"),
        "t": t,
        "mv": mv,
    }


EXCEL_EXTS = (".xlsx", ".xlsm")

# Metadata-sheet column (lower case) -> key used by field(), the same keys as the CSV header
META_KEYS = {
    "run #": "run", "run": "run",
    "experiment name": "experiment", "experiment": "experiment",
    "date collected": "started",
    "sample rate (hz)": "sample_rate_hz",
    "data file": "data_file",
    "pmt gain (mv)": "pmt_gain_mv",
    "led power (ma)": "led_power_ma",
    "concentration (nm)": "concentration_nm",
    "collection duration (s)": "duration_s",
    "recording delay (s)": "recording_delay_s",
    "recipe": "recipe_name",
    "recipe step": "step",
    "channel": "channel",
    "record while step": "record_while_step",
    "stopped early": "stopped_early",
}
# Readouts-sheet column (lower case) -> footer key, the same keys as the CSV footer
READOUT_KEYS = {
    "max (mv)": "max_mv", "min (mv)": "min_mv", "average (mv)": "mean_mv",
    "mean (mv)": "mean_mv", "std dev (mv)": "std_mv", "duration (s)": "duration_s",
    "timestamp": "timestamp",
}


def _cell_text(v):
    """Excel cell -> text for metadata ('' for empty, 400.0 -> '400')."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    if isinstance(v, (float, np.floating)) and float(v).is_integer():
        return str(int(v))
    return str(v).strip()


def _find_sheet(sheets, *names):
    low = {k.strip().lower(): k for k in sheets}
    for n in names:
        if n in low:
            return sheets[low[n]]
    return None


def _rows_by_run(df):
    """Metadata/Readouts sheet -> {run text: row dict}, plus the rows in order."""
    if df is None or df.empty:
        return {}, []
    run_col = next((c for c in df.columns if str(c).strip().lower() in ("run #", "run")), None)
    rows = [r for r in df.to_dict("records") if any(_cell_text(v) for v in r.values())]
    by_run = {_cell_text(r[run_col]): r for r in rows} if run_col is not None else {}
    return by_run, rows


def _excel_meta(row):
    meta = {}
    if not row:
        return meta
    low = {str(k).strip().lower(): v for k, v in row.items()}
    for col, v in row.items():
        key = META_KEYS.get(str(col).strip().lower())
        if key is None and str(col).strip().lower() in ("led channel", "led power (%)", "x", "y", "z"):
            continue                                   # combined below, like the CSV header
        if _cell_text(v):
            meta[key or str(col).strip()] = _cell_text(v)
    if _cell_text(low.get("led channel")):
        pct = _cell_text(low.get("led power (%)"))
        meta["led"] = _cell_text(low["led channel"]) + (f" at {pct}%" if pct else "")
    xyz = [_cell_text(low.get(k)) for k in ("x", "y", "z")]
    if any(xyz):
        meta["position_mm"] = " ".join(x or "–" for x in xyz)
    return meta


def _excel_summary(row, n):
    summary = {"samples": str(n)}
    for col, v in (row or {}).items():
        key = READOUT_KEYS.get(str(col).strip().lower())
        if key and _cell_text(v):
            summary[key] = _cell_text(v)
    return summary


def parse_instrument_excel(path):
    """One dict per signal column (same shape as parse_instrument_csv returns)."""
    try:
        sheets = pd.read_excel(path, sheet_name=None, engine="openpyxl")
    except PermissionError:
        raise ReadError("The workbook is locked. Close it in Excel (or any other program) and try again.")
    except ImportError:
        raise ReadError("Reading Excel files needs openpyxl:  pip install openpyxl")
    except Exception as e:
        raise ReadError(f"The workbook couldn't be read: {e}")

    data = _find_sheet(sheets, "data", "raw data")
    if data is None:                                   # first sheet that isn't Metadata/Readouts
        data = next((df for name, df in sheets.items()
                     if name.strip().lower() not in ("metadata", "readouts")), None)
    if data is None or data.shape[1] < 2:
        raise ReadError("Couldn't find a data sheet with a time column and at least one signal column.")
    meta_by_run, meta_rows = _rows_by_run(_find_sheet(sheets, "metadata"))
    read_by_run, read_rows = _rows_by_run(_find_sheet(sheets, "readouts"))

    cols = [str(c).strip() for c in data.columns]
    time = pd.to_numeric(data.iloc[:, 0], errors="coerce").to_numpy(dtype="float64")
    tv = time[~np.isnan(time)]
    if tv.size == 0:
        raise ReadError(f"The first column ('{cols[0]}') of the data sheet has no numbers; it should be time.")
    dt_time = float(np.median(np.diff(tv))) if tv.size > 1 else np.nan

    book = os.path.basename(path)
    out = []
    for j in range(1, data.shape[1]):
        v = pd.to_numeric(data.iloc[:, j], errors="coerce").to_numpy(dtype="float64")
        ok = ~np.isnan(v)
        if not ok.any():
            continue
        # "Channel 12 raw signal (mV)" is run 12; otherwise match the metadata rows in order
        m = re.search(r"(?:channel|run|ch)\s*#?\s*(\d+)", cols[j], re.I)
        run = m.group(1) if m else None
        mrow = meta_by_run.get(run) if run else None
        rrow = read_by_run.get(run) if run else None
        if mrow is None and j - 1 < len(meta_rows):
            mrow = meta_rows[j - 1]
        if rrow is None and j - 1 < len(read_rows):
            rrow = read_rows[j - 1]
        meta = _excel_meta(mrow)
        if "run" not in meta and run:
            meta["run"] = run
        meta.setdefault("experiment", os.path.splitext(book)[0])
        meta["sheet_column"] = cols[j]

        # Columns can be longer than the time column: carry on at the sample rate
        n = int(np.flatnonzero(ok)[-1]) + 1
        try:
            dt = 1.0 / float(meta.get("sample_rate_hz", ""))
        except (ValueError, ZeroDivisionError):
            dt = dt_time
        t = tv[0] + np.arange(n) * (dt if np.isfinite(dt) else 1.0)
        k = min(n, len(time))
        t[:k] = np.where(np.isnan(time[:k]), t[:k], time[:k])
        t, mv = t[ok[:n]], v[:n][ok[:n]]
        if np.any(np.diff(t) < 0):
            order = np.argsort(t, kind="stable")
            t, mv = t[order], mv[order]

        out.append({
            "path": path,
            "file": meta.get("data_file") or cols[j],
            "folder": book,
            "name": f"run {meta['run']}" if "run" in meta else cols[j],
            "meta": meta,
            "summary": _excel_summary(rrow, len(t)),
            "t_label": _pretty_label(cols[0], "Time (s)"),
            "v_label": "Signal (mV)" if "mv" in cols[j].lower() else cols[j],
            "t": t,
            "mv": mv,
        })
    if not out:
        raise ReadError("The data sheet has no numeric signal columns.")
    return out


def parse_file(path):
    """List of trace dicts: one for a CSV file, one per signal column for a workbook."""
    if path.lower().endswith(EXCEL_EXTS):
        return parse_instrument_excel(path)
    if path.lower().endswith(".xls"):
        raise ReadError("Old .xls workbooks aren't supported. Save it as .xlsx in Excel and add it again.")
    return [parse_instrument_csv(path)]


def _pretty_label(col, default):
    c = col.lower()
    if c in ("time_s", "time (s)", "time"):
        return "Time (s)"
    if c in ("value_mv", "mv", "value (mv)"):
        return "Signal (mV)"
    return col or default


def window_slice(t, t0, t1):
    """Index range [i0, i1) of the sorted time array inside t0 <= t <= t1."""
    return int(np.searchsorted(t, t0, "left")), int(np.searchsorted(t, t1, "right"))


def window_stats(t, mv, t0, t1, population=True):
    """Max, min, mean, std of mV where t0 <= t <= t1 (every point is used).

    population=True (divide by n) matches the std_mv the instrument writes in the footer."""
    i0, i1 = window_slice(t, t0, t1)
    seg = mv[i0:i1]
    n = len(seg)
    ddof = 0 if population else 1
    if n == 0:
        return {"Points": 0, "Max": np.nan, "Min": np.nan, "Mean": np.nan, "Std": np.nan,
                "Time of max": np.nan, "Time of min": np.nan}
    imax, imin = int(seg.argmax()), int(seg.argmin())
    return {
        "Points": n,
        "Max": float(seg[imax]), "Min": float(seg[imin]),
        "Mean": float(seg.mean()),
        "Std": float(seg.std(ddof=ddof)) if n > ddof else np.nan,
        "Time of max": float(t[i0 + imax]), "Time of min": float(t[i0 + imin]),
    }


def cache_summary(entries, size_bytes):
    """"1 run · 35 MB" / "160 runs · 5.6 GB" (MB below 1 GB)."""
    runs = f"{entries} run" + ("" if entries == 1 else "s")
    if size_bytes >= 1e9:
        return f"{runs} · {size_bytes / 1e9:.1f} GB"
    return f"{runs} · {size_bytes / 1e6:.0f} MB"


def eta_text(seconds):
    """'45 s' under 90 s, else whole minutes."""
    return f"{seconds:.0f} s" if seconds < 90 else f"{round(seconds / 60)} min"


def draw_budget(n_traces, width_px=1200):
    """(raw_limit, bins) for view_points so the chart stays quick with many traces.

    One min/max pair per screen pixel is enough for the band; with many overlapping traces
    the slices get wider (spikes are still kept), and the total number of raw points drawn
    is capped, so 160 traces cost about as much as a dozen."""
    bins = int(max(400, min(3000, width_px * min(1.0, math.sqrt(20 / max(1, n_traces))))))
    raw_limit = max(2 * bins, min(12000, 300_000 // max(1, n_traces)))
    return raw_limit, bins


def view_points(t, mv, x0, x1, raw_limit=12000, bins=3000):
    """Points to draw for the visible range. Returns (t, mv, every_point_shown).

    If there are more than raw_limit points visible, keep only the min and max of
    each of `bins` equal slices, in time order, so the shape and all spikes stay visible."""
    i0, i1 = window_slice(t, x0, x1)
    i0, i1 = max(0, i0 - 1), min(len(t), i1 + 1)
    m = i1 - i0
    if m <= raw_limit:
        return t[i0:i1], mv[i0:i1], True
    k = m // bins
    end = i0 + k * bins
    ys = mv[i0:end].reshape(bins, k)
    amin, amax = ys.argmin(axis=1), ys.argmax(axis=1)
    base = i0 + np.arange(bins) * k
    idx = np.column_stack([base + np.minimum(amin, amax), base + np.maximum(amin, amax)]).ravel()
    if end < i1:
        idx = np.append(idx, [end + int(mv[end:i1].argmin()), end + int(mv[end:i1].argmax())])
        idx[-2:] = np.sort(idx[-2:])
    return t[idx], mv[idx], False


def fmt(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    if isinstance(v, (int, np.integer)):
        return f"{v:,}"
    a = abs(v)
    if a != 0 and (a >= 1e7 or a < 1e-4):
        return f"{v:.4e}"
    return f"{v:,.6g}" if a < 1000 else f"{v:,.2f}"


def field(f, *keys):
    """First matching metadata value (case-insensitive key match), else ''."""
    for d in (f["meta"], f["summary"]):
        low = {k.lower(): v for k, v in d.items()}
        for k in keys:
            if k in low:
                return low[k]
    return ""


def is_stopped(f):
    return str(field(f, "stopped_early", "stopped early")).strip().lower() in ("yes", "y", "true", "1")


SORT_MODES = ["Run # ↑", "Run # ↓", "Load order"]
_LAST_NUMBER_RE = re.compile(r"(\d+)(?!.*\d)")


def run_number(f):
    """Run # as an int: from the file's metadata, else the last number in its file name; None if neither."""
    m = re.search(r"\d+", str(f["meta"].get("run", "")))
    if m:
        return int(m.group())
    m = _LAST_NUMBER_RE.search(os.path.splitext(f["file"])[0])
    return int(m.group(1)) if m else None


def sorted_files(files, mode):
    """New list in the chosen order. Numeric run order (9 before 10); runs without a number go last
    in either direction; equal run numbers are ordered by folder, then file, then load order."""
    if mode not in SORT_MODES[:2]:
        return sorted(files, key=lambda f: f.get("load_idx", 0))
    numbered = [f for f in files if run_number(f) is not None]
    rest = [f for f in files if run_number(f) is None]
    tie = lambda f: (f["folder"].lower(), f["file"].lower(), f.get("load_idx", 0))  # noqa: E731
    numbered.sort(key=tie)                                         # stable sorts: tie-break first,
    numbered.sort(key=run_number, reverse=(mode == SORT_MODES[1]))  # then by run in either direction
    return numbered + sorted(rest, key=tie)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
COLORS = ["#2563eb", "#ea580c", "#059669", "#7c3aed", "#d62728",
          "#17becf", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22",
          "#393b79", "#ad494a", "#637939", "#7b4173", "#e6550d",
          "#3182bd", "#843c39", "#31a354", "#de9ed6", "#8c6d31"]

LEGEND_MODES = ["Auto", "Run", "Run + experiment", "Folder / file", "PMT gain",
                "Concentration", "Channel", "Position"]
PLOT_STYLES = ["Points", "Line", "Points + line"]
BEST_LEGEND_MAX = 10      # loc="best" scans every plotted point on each redraw; only worth it for a few traces
LEGEND_AUTO_HIDE = 20     # loading past this many traces turns the legend off (tick it to bring it back)

# Theme
ACCENT = "#0f766e"
ACCENT_DARK = "#115e59"
BG = "#eef1f4"            # window background, around the cards
CARD = "#ffffff"
BORDER = "#dde2e8"
TEXT = "#18212b"
MUTED = "#4d5967"
SEL_BG = "#e6f4f2"        # selected file
SHADE = "#e9edf1"         # chart area outside the window
MONO = ("Consolas", 10)

# Metadata key -> name shown in the Details panel
PRETTY = {
    "experiment": "Experiment", "started": "Started", "sample_rate_hz": "Sample rate (Hz)",
    "led": "LED", "led_power_ma": "LED power (mA)", "pmt_gain_mv": "PMT gain (mV)",
    "concentration_nm": "Concentration (nM)", "position_mm": "Position (mm)",
    "recipe": "Recipe", "recipe_name": "Recipe", "step": "Step", "channel": "Channel",
    "data_file": "Data file", "duration_s": "Duration (s)", "recording_delay_s": "Recording delay (s)",
    "record_while_step": "Record while step", "stopped_early": "Stopped early",
    "samples": "Samples", "max_mv": "Max (mV)", "min_mv": "Min (mV)", "mean_mv": "Mean (mV)",
    "std_mv": "Std (mV)", "timestamp": "Timestamp", "smoothing": "Smoothing",
}

# White tick inside the show/hide box (pixel, 2 px tall)
CHECK_PX = [(4, 7), (5, 8), (6, 9), (7, 10), (8, 9), (9, 8), (10, 7), (11, 6), (12, 5), (13, 4)]


def _tint(color, amount):
    """Mix a #rrggbb colour with white (amount 0 = colour, 1 = white)."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(round(c + (255 - c) * amount) for c in (r, g, b))


DROP_EXTS = (".csv", ".txt", ".tsv", ".dat", ".xlsx", ".xlsm")


class NoiseView(tk.Toplevel):
    """Spectrum, spike list and threshold control of one trace (one window per trace)."""
    ZOOM_HALF = 0.002          # s either side of a spike when it is clicked
    PICK_FRACTION = 0.05       # right-click reach, as a fraction of the spectrum plot width

    def __init__(self, app, trace):
        super().__init__(app)
        self.app, self.trace = app, trace
        self.ready = False
        self._closed = False
        self.freqs = self.psd = None
        self.df = None
        self.peaks = []
        self.spikes = None
        meta = app.noise_meta
        if meta is not None:
            self.t0, self.t1, self.k, self.f_min = meta[0], meta[1], meta[2], meta[3]
        else:
            rng = app._get_range(quiet=True) or (trace["data"].t_start, trace["data"].t_end)
            self.t0, self.t1 = rng
            self.k, self.f_min = app.settings["spike_k"], app.settings["f_min_hz"]
        self._q = queue.Queue()
        self._set_title()
        self.geometry("1000x600")
        self.configure(background=CARD)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._start()

    # ---------- layout ----------
    def _build(self):
        top = ttk.Frame(self, style="Card.TFrame", padding=(12, 10, 12, 4))
        top.pack(fill=tk.X)
        self.status_lbl = ttk.Label(top, text="Computing…", style="Muted.Card.TLabel")
        self.status_lbl.pack(side=tk.LEFT)
        self.rerun_btn = ttk.Button(top, text="Re-run", command=self._on_rerun, state="disabled")
        self.rerun_btn.pack(side=tk.RIGHT)
        self.k_var = tk.StringVar(value=f"{self.k:g}")
        e = self.k_entry = ttk.Entry(top, textvariable=self.k_var, width=6, state="disabled")
        e.pack(side=tk.RIGHT, padx=(0, 6))
        e.bind("<Return>", lambda ev: self._on_rerun())
        ttk.Label(top, text="Spike k", style="Card.TLabel").pack(side=tk.RIGHT, padx=(0, 4))

        body = ttk.Frame(self, style="Card.TFrame", padding=(12, 0, 12, 10))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body, style="Card.TFrame")
        left.grid(row=0, column=0, sticky="nsew")
        self.fig = Figure(figsize=(6, 4), dpi=100, facecolor=CARD)
        self.spec_ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=left)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.res_lbl = ttk.Label(left, text="", style="Muted.Card.TLabel")
        self.res_lbl.pack(anchor="w")
        self.note_lbl = ttk.Label(left, text="", style="Muted.Card.TLabel")
        self.note_lbl.pack(anchor="w")
        self._peaks_lbl = ttk.Label(left, text="", style="Card.TLabel", justify=tk.LEFT)
        self._peaks_lbl.pack(anchor="w", pady=(4, 0))
        self._style_axes()

        right = ttk.Frame(body, style="Card.TFrame")
        right.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        cols = ("Time (s)", "Height (mV)", "Spacing (ms)")
        self.spike_tree = ttk.Treeview(right, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            self.spike_tree.heading(c, text=c, anchor="e")
            self.spike_tree.column(c, width=90, anchor="e", stretch=True)
        ys = ttk.Scrollbar(right, orient=tk.VERTICAL, command=self.spike_tree.yview)
        self.spike_tree.configure(yscrollcommand=ys.set)
        self.spike_tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        self.cap_lbl = ttk.Label(right, text="", style="Muted.Card.TLabel")
        self.cap_lbl.grid(row=1, column=0, columnspan=2, sticky="w")
        self.spike_tree.bind("<ButtonRelease-1>", self._on_tree_click)

    def _style_axes(self):
        ax = self.spec_ax
        ax.set_facecolor(CARD)
        ax.set_xlabel("Frequency (Hz)", color=MUTED)
        ax.set_ylabel("Amplitude (mV RMS per bin)", color=MUTED)
        ax.grid(True, which="major", color="#e7ebef", lw=0.8)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.tick_params(colors=MUTED, labelsize=9)

    def _set_title(self):
        self.title(f"Noise view · {self.app._label(self.trace)} · k = {self.k:g}")

    # ---------- computing ----------
    def _start(self):
        trace, t0, t1, k = self.trace["data"], self.t0, self.t1, self.k

        def work():                          # never touches Tk
            try:
                i0, i1 = trace.index_range(t0, t1)
                spikes = noise.find_spikes(trace, t0, t1, k)
                spec, note = None, ""
                if i1 - i0 < 2:
                    note = noise.NOTE_EMPTY
                elif trace.dt is None:
                    note = noise.NOTE_NOT_UNIFORM
                elif i1 - i0 >= 2:
                    spec = noise.trace_spectrum(trace, i0, i1)
                self._q.put(("ok", (spikes, spec, note)))
            except Exception as e:
                self._q.put(("error", e))

        threading.Thread(target=work, daemon=True).start()
        self.after(50, self._poll)

    def _poll(self):
        if self._closed:
            return
        try:
            kind, val = self._q.get_nowait()
        except queue.Empty:
            self.after(50, self._poll)
            return
        if kind == "error":
            self.status_lbl.config(text=f"Couldn't compute: {val}")
            self.ready = True
            return
        spikes, spec, note = val
        if self.spikes is None:                  # a result for an older k never replaces a newer one
            self.spikes = spikes
        if spec is not None:
            self.freqs, self.psd, self.df = spec
            self.peaks = noise.top_peaks(self.freqs, self.psd, self.df, f_min=self.f_min)
        self.note_lbl.config(text=note)
        self.status_lbl.config(text=f"Window {self.t0:g} – {self.t1:g} s")
        self._draw_spectrum()
        self._fill_spikes()
        self.ready = True
        self.rerun_btn.config(state="normal")
        self.k_entry.config(state="normal")
        self.app._redraw_threshold()

    def _draw_spectrum(self):
        ax = self.spec_ax
        ax.clear()
        self._style_axes()
        if self.freqs is not None:
            amp = noise.amplitude_spectrum(self.psd, self.df)
            ax.loglog(self.freqs[1:], amp[1:], color=ACCENT, lw=1)
            for hz, mv in self.peaks:
                ax.plot([hz], [amp[min(int(round(hz / self.df)), len(amp) - 1)]], marker="v",
                        ls="none", color="#b42318", ms=7)
            self.res_lbl.config(text=f"Resolution {self.df:.1f} Hz")
            self._peaks_lbl.config(text=self.peak_text())
        else:
            self.res_lbl.config(text="")
            self._peaks_lbl.config(text="")
        self._draw_watch()

    def _draw_watch(self):
        ax = self.spec_ax
        for ln in [ln for ln in ax.lines if ln.get_linestyle() == "--"]:
            ln.remove()
        if self.freqs is not None:
            for hz in self.app.settings["watch_hz"]:
                ax.axvline(hz, color="#6b7280", lw=1, ls="--")
        self._relayout()
        self.canvas.draw_idle()

    def _relayout(self):
        try:
            self.fig.tight_layout()
        except Exception:
            pass

    def peak_text(self):
        return "\n".join(f"#{i}  {hz:.1f} Hz  {mv:.3f} mV RMS"
                         for i, (hz, mv) in enumerate(self.peaks, 1))

    def _fill_spikes(self):
        tree = self.spike_tree
        tree.delete(*tree.get_children())
        sp = self.spikes
        n = len(sp.times)
        shown = min(n, noise.SPIKE_LIST_CAP)
        t, h = sp.times[:shown], sp.heights[:shown]
        for i in range(shown):
            gap = f"{(t[i] - t[i - 1]) * 1000:.3f}" if i else ""
            tree.insert("", tk.END, values=(f"{t[i]:.6f}", f"{h[i]:.4f}", gap))
        self.cap_lbl.config(text=f"Showing the first {noise.SPIKE_LIST_CAP:,} of {n:,} spikes."
                            if n > noise.SPIKE_LIST_CAP else "")

    # ---------- actions ----------
    def _on_tree_click(self, event):
        iid = self.spike_tree.identify_row(event.y)
        if iid:
            self.goto_spike(self.spike_tree.index(iid))

    def goto_spike(self, index):
        """Centre the main chart on a spike (±2 ms, Back returns) and select its trace."""
        if self.spikes is None or not 0 <= index < len(self.spikes.times):
            return
        t = float(self.spikes.times[index])
        app = self.app
        if app.tools:
            app.tools.zoom_x(t - self.ZOOM_HALF, t + self.ZOOM_HALF)
        app._select_file(self.trace)

    def _on_rerun(self):
        try:
            k = float(self.k_var.get())
            if not math.isfinite(k):
                raise ValueError
        except ValueError:
            self.status_lbl.config(text=f'Spike k "{self.k_var.get()}" is not a number.')
            return
        if not 0.5 <= k <= 1000:
            self.status_lbl.config(text=f'Spike k "{self.k_var.get()}" must be between 0.5 and 1000.')
            return
        self.rerun(k)

    def rerun(self, k):
        """Re-detect spikes of this trace with a new k (spectrum and settings stay as they are)."""
        if not self.ready:
            return
        self.k = float(k)
        self.spikes = noise.find_spikes(self.trace["data"], self.t0, self.t1, self.k)
        self._fill_spikes()
        self._set_title()
        self.k_var.set(f"{self.k:g}")
        self.status_lbl.config(text=f"Window {self.t0:g} – {self.t1:g} s")
        self.app._update_noise_row(self.trace, self.spikes, (self.t0, self.t1), self.k)
        self.app._redraw_threshold()

    def add_watch(self, freq):
        self.app.add_watch_frequency(freq)

    def peak_near(self, x_pixel):
        """Frequency of the peak marker nearest to a display x within the reach, else None."""
        if not self.peaks:
            return None
        ax = self.spec_ax
        best, best_d = None, self.PICK_FRACTION * ax.bbox.width
        for hz, _ in self.peaks:
            d = abs(ax.transData.transform((hz, 1.0))[0] - x_pixel)
            if d <= best_d:
                best, best_d = hz, d
        return best

    def _on_press(self, event):
        if event.button != 3 or event.inaxes is not self.spec_ax:
            return
        hz = self.peak_near(event.x)
        if hz is None:
            return
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Add to watch-list", command=lambda: self.add_watch(hz))
        try:
            menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            menu.grab_release()

    def close(self):
        self.destroy()

    def destroy(self):
        if not self._closed:
            self._closed = True
            self.app._noise_view_closed(self)
            try:
                self.canvas.get_tk_widget().destroy()
                self.fig.clear()
            except tk.TclError:
                pass
        super().destroy()


class App(TkBase):
    FILE_COLS = ("Conc. (nM)", "Points")
    RES_HEAD = ("Trace", "Range (min to max, dot = mean)", "Points", "Mean", "Std", "Max", "Min",
                "Time of max (s)")

    def __init__(self, settings_path=None):
        super().__init__()
        self.title("Multi-File Analyzer")
        self.geometry("1400x880")
        self.minsize(1100, 700)
        self.files = []          # parsed dicts, each also has "show" and "color"
        self.results = None
        self._result_files = []  # the file behind each results row
        self.tools = None        # ChartTools (toolbar, zoom modes, history)
        self._lines = []         # (Line2D, file) currently on the chart
        self._legend = None
        self._res_rows = []      # (file, cells, name label) for each statistics row
        self._auto_ctx = None    # cached (several experiments, several folders) for Auto labels
        self._icons = {}         # (colour, shown) -> PhotoImage for the file list
        self._xlim_job = None
        self._loading = False
        self._load_seq = 0       # counts every trace ever added, for "Load order" sorting
        self._q = queue.Queue()
        self.cache_dir = default_cache_dir()
        self._memory_noted = False   # the "cache folder unwritable" note is shown once per session
        self.settings_path = settings_path or default_settings_path()
        self.settings = load_settings(self.settings_path)
        self.noise_rows = []         # (trace dict, NoiseResult | Exception), in the order analysed
        self.noise_meta = None       # (t0, t1, k, f_min, watch, resolution) of the last run
        self.noise_outdated = False
        self._noise_running = False
        self._noise_q = queue.Queue()
        self._noise_cancel = threading.Event()
        self._noise_n = 0
        self._noise_files = []
        self._noise_views = {}       # id(trace dict) -> open NoiseView
        self._nset = None            # the open Noise settings dialog, if any
        self._build_ui()
        if HAS_DND:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)

    # ---------- layout ----------
    def _build_ui(self):
        self.configure(bg=BG)
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
            tkfont.nametofont(name).configure(size=10)
        self._font = tkfont.nametofont("TkDefaultFont")
        self._bold = self._font.copy(); self._bold.configure(weight="bold")
        self._small = self._font.copy(); self._small.configure(size=9)
        self._small_bold = self._font.copy(); self._small_bold.configure(size=9, weight="bold")
        self._mono = tkfont.Font(self, family=MONO[0], size=MONO[1])
        self._mono_bold = tkfont.Font(self, family=MONO[0], size=MONO[1], weight="bold")
        self._style()

        self.style_var = tk.StringVar(value="Points")
        self.legend_var = tk.StringVar(value="Auto")
        self.sort_var = tk.StringVar(value=SORT_MODES[0])
        self.legend_on = tk.BooleanVar(value=True)
        self.t0_var, self.t1_var = tk.StringVar(), tk.StringVar()
        self.sample_var = tk.BooleanVar(value=False)
        self.visible_only = tk.BooleanVar(value=True)
        for v in (self.t0_var, self.t1_var, self.visible_only):
            v.trace_add("write", lambda *a: self._mark_noise_outdated())

        self._build_menu()
        self._build_toolbar()

        status = tk.Frame(self, bg=CARD)
        status.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Frame(status, bg=BORDER, height=1).pack(fill=tk.X)
        self.status = ttk.Label(status, text="Add or drop CSV files or Excel workbooks to begin." if HAS_DND
                                else "Add CSV files or Excel workbooks to begin.",
                                style="Muted.Card.TLabel", anchor="w", padding=(16, 3))
        self.status.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.progress = ttk.Progressbar(status, mode="determinate", length=160)   # packed only while loading
        self.cancel_btn = ttk.Button(status, text="Cancel", style="Link.TButton",
                                     command=self.cancel_noise)                    # packed only during noise analysis

        body = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)
        body.add(self._build_sidebar(body), weight=0)
        main = ttk.Frame(body, padding=(16, 14))
        body.add(main, weight=1)
        split = ttk.PanedWindow(main, orient=tk.VERTICAL)
        split.pack(fill=tk.BOTH, expand=True)
        split.add(self._build_chart(split), weight=3)
        split.add(self._build_results(split), weight=2)
        self._render_results()

    def _style(self):
        s = ttk.Style(self)
        if "clam" in s.theme_names():
            s.theme_use("clam")
        flat = dict(bordercolor="#c9d1da", lightcolor=CARD, darkcolor=CARD)
        s.configure(".", background=BG, foreground=TEXT, focuscolor=ACCENT)
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=CARD)
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("Card.TLabel", background=CARD)
        s.configure("Muted.Card.TLabel", background=CARD, foreground=MUTED)
        s.configure("Head.Card.TLabel", background=CARD, font=self._bold)
        s.configure("TButton", background=CARD, padding=(12, 5), **flat)
        s.map("TButton", background=[("pressed", "#e4e8ec"), ("active", "#f3f5f7")])
        s.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", font=self._bold,
                    padding=(16, 5), bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT)
        s.map("Accent.TButton", background=[("pressed", ACCENT_DARK), ("active", ACCENT_DARK)],
              bordercolor=[("active", ACCENT_DARK)], lightcolor=[("active", ACCENT_DARK)],
              darkcolor=[("active", ACCENT_DARK)])
        s.configure("Link.TButton", background=CARD, foreground=ACCENT, padding=(6, 2), borderwidth=0,
                    bordercolor=CARD, lightcolor=CARD, darkcolor=CARD)
        s.map("Link.TButton", foreground=[("active", ACCENT_DARK)], background=[("active", CARD)])
        s.configure("Card.TCheckbutton", background=CARD, indicatorbackground=CARD)
        s.map("Card.TCheckbutton", background=[("active", CARD)],
              indicatorbackground=[("selected", ACCENT), ("", CARD)],
              indicatorforeground=[("", "#ffffff")])
        s.configure("TEntry", fieldbackground=CARD, padding=(6, 4), **flat)
        s.map("TEntry", bordercolor=[("focus", ACCENT)])
        s.configure("TCombobox", fieldbackground=CARD, background=CARD, arrowcolor=MUTED, **flat)
        s.map("TCombobox", fieldbackground=[("readonly", CARD)], selectbackground=[("readonly", CARD)],
              selectforeground=[("readonly", TEXT)])
        s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT, rowheight=28,
                    borderwidth=0, bordercolor=CARD, lightcolor=CARD, darkcolor=CARD)
        s.map("Treeview", background=[("selected", SEL_BG)], foreground=[("selected", TEXT)])
        s.configure("Treeview.Heading", background=CARD, foreground=MUTED, font=self._small_bold,
                    relief="flat", bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD)
        s.map("Treeview.Heading", background=[("active", "#f3f5f7")])
        s.configure("TPanedwindow", background=BG)
        s.configure("Sash", sashthickness=12, gripcount=0, background=BG,
                    lightcolor=BG, darkcolor=BG, bordercolor=BG)
        s.configure("TSeparator", background=BORDER)

    def _card(self, parent):
        return tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightcolor=BORDER,
                        highlightthickness=1)

    def show_cache_dialog(self):
        dlg = tk.Toplevel(self)
        dlg.title("Cache")
        dlg.transient(self)
        dlg.grab_set()
        body = ttk.Frame(dlg, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="Location").pack(anchor="w")
        loc = tk.StringVar(value=self.cache_dir)
        ttk.Entry(body, textvariable=loc, state="readonly", width=60).pack(fill=tk.X, pady=(0, 8))
        size_lbl = ttk.Label(body)
        size_lbl.pack(anchor="w", pady=(0, 8))
        result = ttk.Label(body, wraplength=420, justify="left")
        result.pack(anchor="w", pady=(0, 8))

        def refresh():
            n, b = cache_info(self.cache_dir)
            size_lbl.config(text=cache_summary(n, b))

        def open_folder():
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                os.startfile(self.cache_dir)
            except (OSError, AttributeError) as e:
                messagebox.showerror("Cache", f"Could not open the folder: {e}", parent=dlg)

        def clear():
            if self._loading:
                messagebox.showinfo("Cache", "Files are loading; try again when loading has finished.",
                                    parent=dlg)
                return
            if not messagebox.askyesno("Clear cache", "Delete all cached traces? They are rebuilt the "
                                       "next time a file is opened.", parent=dlg):
                return
            r, k = clear_cache(self.cache_dir)
            msg = f"Removed {r}."
            if k:
                msg = f"Removed {r}. Kept {k} in use by loaded traces — Clear all, then clear the cache again."
            result.config(text=msg)
            refresh()

        btns = ttk.Frame(body)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Open folder", command=open_folder).pack(side=tk.LEFT)
        clear_btn = ttk.Button(btns, text="Clear cache", command=clear,
                               state="disabled" if self._loading else "normal")
        clear_btn.pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Close", command=dlg.destroy).pack(side=tk.RIGHT)
        refresh()
        return dlg

    def _build_menu(self):
        bar = tk.Menu(self)
        m = tk.Menu(bar, tearoff=False)
        m.add_command(label="Add files…", accelerator="Ctrl+O", command=self.add_files)
        m.add_command(label="Rename trace", accelerator="F2", command=self.rename_selected)
        m.add_command(label="Remove selected", accelerator="Del", command=self.remove_selected)
        m.add_command(label="Clear all", command=self.clear_all)
        self._file_menu = m
        m.add_command(label="Cache…", command=self.show_cache_dialog)
        m.add_separator()
        m.add_command(label="Exit", command=self.destroy)
        bar.add_cascade(label="File", menu=m)

        m = tk.Menu(bar, tearoff=False)
        m.add_command(label="Show all", command=lambda: self.set_all_shown(True))
        m.add_command(label="Hide all", command=lambda: self.set_all_shown(False))
        m.add_separator()
        sub = tk.Menu(m, tearoff=False)
        for v in PLOT_STYLES:
            sub.add_radiobutton(label=v, value=v, variable=self.style_var, command=self.draw)
        m.add_cascade(label="Plot style", menu=sub)
        sub = tk.Menu(m, tearoff=False)
        for v in LEGEND_MODES:
            sub.add_radiobutton(label=v, value=v, variable=self.legend_var, command=self._on_legend)
        m.add_cascade(label="Legend", menu=sub)
        sub = tk.Menu(m, tearoff=False)
        for v in SORT_MODES:
            sub.add_radiobutton(label=v, value=v, variable=self.sort_var, command=self.apply_sort)
        m.add_cascade(label="Sort traces", menu=sub)
        m.add_checkbutton(label="Show legend", variable=self.legend_on, accelerator="Ctrl+L",
                          command=self._on_legend_toggle)
        m.add_command(label="Reset zoom", accelerator="Home", command=self.reset_zoom)
        m.add_separator()
        m.add_command(label="Noise view…", command=self.open_selected_noise_view)
        m.add_command(label="Noise settings…", command=self.show_noise_settings)
        m.add_separator()
        m.add_command(label="Smooth selected trace…", command=self.open_smoothing)
        bar.add_cascade(label="View", menu=m)

        m = tk.Menu(bar, tearoff=False)
        m.add_command(label="Copy results for Excel", command=self.copy_results)
        m.add_command(label="Export results…", command=self.export_results)
        m.add_command(label="Export window data…", command=self.export_window_data)
        bar.add_cascade(label="Export", menu=m)

        m = tk.Menu(bar, tearoff=False)
        m.add_command(label="About", command=lambda: messagebox.showinfo(
            "About", "Multi-File Analyzer\n\nLoad BB DAQ run CSV files or Excel workbooks, pick a time "
                     "window, and get max, min, mean and standard deviation of every trace in it.\n\n"
                     "Click the box next to a trace to show or hide it (or press Space). "
                     "Drag across the chart to set the window."))
        bar.add_cascade(label="Help", menu=m)
        self.config(menu=bar)
        self.bind("<Control-o>", lambda e: self.add_files())
        self.bind("<Control-l>", lambda e: (self.legend_on.set(not self.legend_on.get()),
                                            self._on_legend_toggle()))

    def _build_toolbar(self):
        bar = ttk.Frame(self, style="Card.TFrame", padding=(16, 10))
        bar.pack(fill=tk.X)
        tk.Frame(self, bg=BORDER, height=1).pack(fill=tk.X)
        self.add_btn = ttk.Button(bar, text="+ Add files", command=self.add_files)
        self.add_btn.pack(side=tk.LEFT)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=14)
        ttk.Label(bar, text="Window", style="Head.Card.TLabel").pack(side=tk.LEFT, padx=(0, 10))
        for label, var in (("from", self.t0_var), ("to", self.t1_var)):
            ttk.Label(bar, text=label, style="Muted.Card.TLabel").pack(side=tk.LEFT)
            e = ttk.Entry(bar, textvariable=var, width=10, font=MONO)
            e.pack(side=tk.LEFT, padx=(6, 10))
            e.bind("<Return>", lambda ev: self.calculate())
        ttk.Label(bar, text="s", style="Muted.Card.TLabel").pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(bar, text="Full range", command=self.full_range).pack(side=tk.LEFT)
        self.calc_btn = ttk.Button(bar, text="Calculate", style="Accent.TButton", command=self.calculate)
        self.calc_btn.pack(side=tk.LEFT, padx=8)
        self.noise_btn = ttk.Button(bar, text="Analyze noise", command=self.analyze_noise)
        self.noise_btn.pack(side=tk.LEFT)
        ttk.Button(bar, text="Smooth…", command=self.open_smoothing).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Checkbutton(bar, text="Shown files only", style="Card.TCheckbutton",
                        variable=self.visible_only, command=self._recalc_if_done).pack(side=tk.RIGHT)
        ttk.Checkbutton(bar, text="Sample std (n−1)", style="Card.TCheckbutton",
                        variable=self.sample_var, command=self._recalc_if_done).pack(side=tk.RIGHT, padx=16)

    def _build_sidebar(self, parent):
        side = tk.Frame(parent, bg=CARD, width=320)
        hdr = ttk.Frame(side, style="Card.TFrame", padding=(14, 12, 14, 6))
        hdr.pack(fill=tk.X)
        ttk.Label(hdr, text="Files", style="Head.Card.TLabel").pack(side=tk.LEFT)
        cb = ttk.Combobox(hdr, textvariable=self.sort_var, state="readonly", width=10, values=SORT_MODES)
        cb.pack(side=tk.LEFT, padx=(12, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self.apply_sort())
        self.count_lbl = ttk.Label(hdr, text="", style="Muted.Card.TLabel")
        self.count_lbl.pack(side=tk.RIGHT)

        btns = ttk.Frame(side, style="Card.TFrame", padding=(14, 10))
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(btns, text="Remove", command=self.remove_selected).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.clear_btn = ttk.Button(btns, text="Clear all", command=self.clear_all)
        self.clear_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))
        tk.Frame(side, bg=BORDER, height=1).pack(fill=tk.X, side=tk.BOTTOM)

        tf = ttk.Frame(side, style="Card.TFrame", padding=(6, 0))
        tf.pack(fill=tk.X)
        self.ftree = ttk.Treeview(tf, columns=self.FILE_COLS, show="tree headings", height=8,
                                  selectmode="extended")
        self.ftree.heading("#0", text="Trace", anchor="w")
        self.ftree.column("#0", width=150, minwidth=120, stretch=True)
        self.ftree.heading("Conc. (nM)", text="Conc. (nM)")
        self.ftree.column("Conc. (nM)", width=72, anchor="center", stretch=False)
        self.ftree.heading("Points", text="Points", anchor="e")
        self.ftree.column("Points", width=72, anchor="e", stretch=False)
        ys = ttk.Scrollbar(tf, orient=tk.VERTICAL, command=self.ftree.yview)
        xs = ttk.Scrollbar(tf, orient=tk.HORIZONTAL, command=self.ftree.xview)
        self.ftree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.ftree.grid(row=0, column=0, sticky="nsew"); ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        tf.columnconfigure(0, weight=1)
        self.ftree.bind("<Button-1>", self._on_tree_click)
        self.ftree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self.ftree.bind("<space>", lambda e: self.toggle_selected())
        self.ftree.bind("<Delete>", lambda e: self.remove_selected())
        self.ftree.bind("<Double-Button-1>", self._on_tree_double)
        self.ftree.bind("<F2>", lambda e: self.rename_selected())
        self.ftree.tag_configure("stopped", foreground="#b42318")

        tk.Frame(side, bg=BORDER, height=1).pack(fill=tk.X, padx=14, pady=(8, 10))
        self.info = tk.Text(side, wrap="word", relief="flat", bg=CARD, fg=TEXT, font=self._font,
                            highlightthickness=0, padx=14, pady=0, height=12, width=36, cursor="arrow",
                            tabs=(150,))
        self.info.tag_configure("head", font=self._bold, spacing3=8)
        self.info.tag_configure("sub", font=self._bold, spacing1=10, spacing3=6)
        self.info.tag_configure("key", foreground=MUTED)
        self.info.tag_configure("line", lmargin2=150, spacing3=3)
        self.info.tag_configure("muted", foreground=MUTED, font=self._small)
        self.info.pack(fill=tk.BOTH, expand=True)
        self.show_info()
        return side

    def _build_chart(self, parent):
        card = self._card(parent)
        top = ttk.Frame(card, style="Card.TFrame", padding=(12, 10, 12, 0))
        top.pack(fill=tk.X)
        ttk.Label(top, text="Superimposed traces", style="Head.Card.TLabel").pack(side=tk.LEFT)
        cb = ttk.Combobox(top, textvariable=self.legend_var, state="readonly", width=17, values=LEGEND_MODES)
        cb.pack(side=tk.RIGHT, padx=(2, 12))
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_legend())
        ttk.Checkbutton(top, text="Legend", style="Card.TCheckbutton", variable=self.legend_on,
                        command=self._on_legend_toggle).pack(side=tk.RIGHT)
        cb = ttk.Combobox(top, textvariable=self.style_var, state="readonly", width=13, values=PLOT_STYLES)
        cb.pack(side=tk.RIGHT, padx=(6, 12))
        cb.bind("<<ComboboxSelected>>", lambda e: self.draw())
        ttk.Label(top, text="Style", style="Muted.Card.TLabel").pack(side=tk.RIGHT)
        self.view_lbl = ttk.Label(card, text="", style="Muted.Card.TLabel", wraplength=1000, padding=(12, 2, 12, 0))
        self.view_lbl.pack(anchor="w")
        if HAS_MPL:
            self.fig = Figure(figsize=(8, 4.2), dpi=100, facecolor=CARD)
            self.ax = self.fig.add_subplot(111)
            self.canvas = FigureCanvasTkAgg(self.fig, master=card)
            self.canvas.mpl_connect("resize_event", self._relayout)
            self.tools = ChartTools(card, self.ax, self.canvas, on_window_selected=self._on_span,
                                    on_reset=lambda: self.draw(reset_view=True),
                                    autoscale_y=self._autoscale_y,
                                    get_selected_trace=lambda: next(iter(self._selected_files()), None),
                                    get_shown_traces=lambda: [f for f in self.files if f["show"]],
                                    label_of=self._label,
                                    on_mode_changed=lambda mode: self._sync_legend_drag())
            self.tools.frame.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(2, 6))
            self.tools.readout_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=12)
            self.canvas.get_tk_widget().config(highlightthickness=0, bg=CARD)
            self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 0))
            self._empty_chart("Add one or more CSV or Excel files to see the data.")
        else:
            ttk.Label(card, text="Install matplotlib to see the chart:  pip install matplotlib",
                      style="Card.TLabel", foreground="#a33").pack(pady=40)
        return card

    def _build_results(self, parent):
        card = self._card(parent)
        top = ttk.Frame(card, style="Card.TFrame", padding=(12, 10, 12, 0))
        top.pack(fill=tk.X)
        for text, cmd in (("Export window data…", self.export_window_data),
                          ("Export results…", self.export_results),
                          ("Copy for Excel", self.copy_results)):
            ttk.Button(top, text=text, style="Link.TButton", command=cmd).pack(side=tk.RIGHT)
        self.results_nb = nb = ttk.Notebook(card)
        nb.pack(fill=tk.BOTH, expand=True, padx=(12, 0), pady=(0, 8))
        stats = ttk.Frame(nb, style="Card.TFrame")
        self.noise_tab = ttk.Frame(nb, style="Card.TFrame")
        nb.add(stats, text="Statistics")
        nb.add(self.noise_tab, text="Noise")
        head = ttk.Frame(stats, style="Card.TFrame", padding=(0, 8, 0, 6))
        head.pack(fill=tk.X)
        self.range_lbl = ttk.Label(head, text="Statistics", style="Head.Card.TLabel")
        self.range_lbl.pack(side=tk.LEFT)
        self.range_sub = ttk.Label(head, text="", style="Muted.Card.TLabel")
        self.range_sub.pack(side=tk.LEFT, padx=12)

        # The table is drawn as canvas items, not one widget per cell: hundreds of rows stay quick.
        self.rcanvas = tk.Canvas(stats, bg=CARD, highlightthickness=0, yscrollincrement=28)
        rys = ttk.Scrollbar(stats, orient=tk.VERTICAL, command=self.rcanvas.yview)
        self.rcanvas.configure(yscrollcommand=rys.set)
        rys.pack(side=tk.RIGHT, fill=tk.Y, pady=(0, 8))
        self.rcanvas.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        self.rcanvas.bind("<Button-1>", self._on_res_click)
        self.rcanvas.bind("<Enter>", lambda e: self.rcanvas.bind_all("<MouseWheel>", self._on_wheel))
        self.rcanvas.bind("<Leave>", lambda e: self.rcanvas.unbind_all("<MouseWheel>"))

        nhead = ttk.Frame(self.noise_tab, style="Card.TFrame", padding=(0, 8, 0, 6))
        nhead.pack(fill=tk.X)
        self.noise_sub = ttk.Label(nhead, text="", style="Muted.Card.TLabel")
        self.noise_sub.pack(side=tk.LEFT)
        ttk.Button(nhead, text="Settings…", style="Link.TButton",
                   command=self.show_noise_settings).pack(side=tk.RIGHT)
        self.noise_banner = ttk.Label(self.noise_tab, text="", style="Muted.Card.TLabel",
                                      foreground="#9a5b00", padding=(0, 0, 0, 6))   # packed only when outdated
        self._noise_body = ttk.Frame(self.noise_tab, style="Card.TFrame")
        self._noise_body.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        self.ntree = ttk.Treeview(self._noise_body, show="headings", selectmode="browse")
        nys = ttk.Scrollbar(self._noise_body, orient=tk.VERTICAL, command=self.ntree.yview)
        nxs = ttk.Scrollbar(self._noise_body, orient=tk.HORIZONTAL, command=self.ntree.xview)
        self.ntree.configure(yscrollcommand=nys.set, xscrollcommand=nxs.set)
        self._noise_body.rowconfigure(0, weight=1)
        self._noise_body.columnconfigure(0, weight=1)
        self.ntree.grid(row=0, column=0, sticky="nsew")
        self.ntree.bind("<Double-Button-1>", self._on_noise_double)
        nys.grid(row=0, column=1, sticky="ns")
        nxs.grid(row=1, column=0, sticky="ew")
        self._render_noise()
        return card

    def _on_wheel(self, event):
        top, bottom = self.rcanvas.yview()
        if top > 0 or bottom < 1:
            self.rcanvas.yview_scroll(int(-event.delta / 120), "units")

    # ---------- files ----------
    def add_files(self):
        if self._loading or self._noise_running:
            return
        paths = filedialog.askopenfilenames(
            title="Add CSV files or Excel workbooks",
            filetypes=[("Data files", "*.csv *.txt *.tsv *.dat *.xlsx *.xlsm"),
                       ("CSV / text files", "*.csv *.txt *.tsv *.dat"),
                       ("Excel workbooks", "*.xlsx *.xlsm"), ("All files", "*.*")])
        self._add_new(paths)

    def _on_drop(self, event):
        """Files dropped from Explorer; folders add every data file inside them."""
        if self._loading or self._noise_running:
            return event.action
        paths = []
        for p in self.tk.splitlist(event.data):
            if os.path.isdir(p):
                paths += sorted(os.path.join(p, n) for n in os.listdir(p)
                                if n.lower().endswith(DROP_EXTS))
            elif os.path.isfile(p):
                paths.append(p)
        self._add_new(paths)
        return event.action

    def _add_new(self, paths):
        if not paths:
            return
        seen = {os.path.normcase(os.path.abspath(f["path"])) for f in self.files}
        new = []
        for p in paths:
            key = os.path.normcase(os.path.abspath(p))
            if key not in seen:
                seen.add(key)
                new.append(p)
        paths = new
        if not paths:
            self.status.config(text="Those files are already loaded.")
            return
        self.load_paths(paths)

    def load_paths(self, paths):
        """Parse files on a background thread so big files don't freeze the window."""
        self._loading = True
        self.add_btn.config(state="disabled")
        self.config(cursor="watch")

        cache_dir = self.cache_dir
        self.progress.config(maximum=len(paths), value=0)
        self.progress.pack(side=tk.RIGHT, padx=(0, 16))

        def work():
            converted = []       # seconds each conversion took, for the time estimate
            n = len(paths)
            flags = []           # is each file cached? asked once, up front
            for p in paths:
                try:
                    flags.append(bool(is_cached(p, cache_dir)))
                except Exception:
                    flags.append(False)   # whatever is wrong with it surfaces when it is opened
            uncached_left = flags.count(False)
            for i, p in enumerate(paths):
                cached = flags[i]
                try:
                    if cached:
                        state, eta = "cached", None
                    elif is_cloud_only(p):
                        state, eta = "cloud", None
                    else:
                        state, eta = "converting", None
                        if converted:
                            eta = sum(converted) / len(converted) * uncached_left
                    self._q.put(("progress", (i, n, os.path.basename(p), state, eta)))
                    if not cached:
                        uncached_left -= 1
                    start = time.monotonic()
                    traces, status = open_traces(p, parse_file, cache_dir)
                    if status == "converted":
                        converted.append(time.monotonic() - start)
                    elif status == "memory":
                        self._q.put(("memory", None))
                    for f in traces:
                        self._q.put(("file", f))
                except ReadError as e:
                    self._q.put(("error", f"{os.path.basename(p)}: {e}"))
                except Exception as e:
                    self._q.put(("error", f"{os.path.basename(p)}: {type(e).__name__}: {e}"))
            self._q.put(("done", None))

        self._load_errors, self._load_added, self._load_notes = [], 0, []
        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll_loader)

    def _poll_loader(self):
        done = False
        try:
            while True:
                kind, val = self._q.get_nowait()
                if kind == "progress":
                    i, n, name, state, eta = val
                    self.progress.config(value=i)
                    if state == "cloud":
                        text = f"{name} is in OneDrive only — downloading…"
                    elif state == "cached":
                        text = f"Opening cached {i + 1}/{n}"
                    else:
                        text = f"Converting {i + 1}/{n} · {name}"
                        if eta is not None:
                            text += f" · about {eta_text(eta)} left"
                    self.status.config(text=text)
                elif kind == "memory":
                    if not self._memory_noted:
                        self._memory_noted = True
                        self._load_notes.append("Couldn't write the cache folder; "
                                                "loading in memory (slower, uses more RAM).")
                elif kind == "error":
                    self._load_errors.append(val)
                elif kind == "file":
                    val["show"] = True
                    val["color"] = None              # given in run order once the batch is sorted
                    val["load_idx"] = self._load_seq
                    self._load_seq += 1
                    self.files.append(val)
                    self._load_added += 1
                elif kind == "done":
                    done = True
        except queue.Empty:
            pass
        if not done:
            self.after(100, self._poll_loader)
            return
        self._loading = False
        self.progress.pack_forget()
        self.add_btn.config(state="normal")
        self.config(cursor="")
        legend_note = ""
        if self._load_added:
            self._mark_noise_outdated()
            self.files[:] = sorted_files(self.files, self.sort_var.get())
            for f in sorted_files([f for f in self.files if f["color"] is None], SORT_MODES[0]):
                f["color"] = self._next_color()      # run 1 gets the first colour, run 2 the next, ...
            self._auto_ctx = None
            if len(self.files) - self._load_added <= LEGEND_AUTO_HIDE < len(self.files) and self.legend_on.get():
                self.legend_on.set(False)
                legend_note = " Legend hidden (too many traces); tick Legend or press Ctrl+L to show it."
            self.refresh_files()
            if not self.t0_var.get() or not self.t1_var.get():
                ext = self._time_extent()
                self.t0_var.set(f"{ext[0]:g}"); self.t1_var.set(f"{ext[1]:g}")
            self.draw(reset_view=True)
            self._recalc_if_done(redraw=False)
        self.status.config(text=f"Added {self._load_added} trace(s). {len(self.files)} loaded, "
                                f"{sum(f['n'] for f in self.files):,} points in total." + legend_note
                           + "".join(" " + n for n in self._load_notes))
        if self._load_errors:
            messagebox.showwarning("Some files were skipped", "\n".join(self._load_errors))

    def apply_sort(self):
        """Reorder every trace (file list, legend, chart, statistics) by the Sort choice."""
        if not self.files:
            return
        sel = self._selected_files()
        self.files[:] = sorted_files(self.files, self.sort_var.get())
        self.refresh_files()
        self.ftree.selection_set([str(i) for i, f in enumerate(self.files) if any(f is s for s in sel)])
        self.draw()
        self._recalc_if_done(redraw=False)

    def _next_color(self):
        used = {f["color"] for f in self.files if f["color"]}
        for c in COLORS:
            if c not in used:
                return c
        return COLORS[len(self.files) % len(COLORS)]

    def _show_icon(self, color, shown):
        """Show/hide box plus a line in the trace colour, for the file list."""
        key = (color, shown)
        img = self._icons.get(key)
        if img is None:
            img = tk.PhotoImage(width=40, height=16)
            if shown:
                img.put(ACCENT, to=(1, 1, 16, 16))
                for x, y in CHECK_PX:
                    img.put("#ffffff", to=(x, y, x + 1, y + 2))
            else:
                img.put("#9aa5b1", to=(1, 1, 16, 16))
                img.put(CARD, to=(2, 2, 15, 15))
            img.put(color if shown else _tint(color, 0.65), to=(22, 7, 40, 10))
            self._icons[key] = img
        return img

    def refresh_files(self):
        sel = set(self.ftree.selection())
        self.ftree.delete(*self.ftree.get_children())
        for i, f in enumerate(self.files):
            iid = str(i)
            self.ftree.insert("", tk.END, iid=iid, text=self._label(f),
                              image=self._show_icon(f["color"], f["show"]),
                              values=(field(f, "concentration_nm", "concentration") or "–", f"{f['n']:,}"),
                              tags=("stopped",) if is_stopped(f) else ())
            if iid in sel:
                self.ftree.selection_add(iid)
        shown = sum(f["show"] for f in self.files)
        self.count_lbl.config(text=f"{shown} of {len(self.files)} shown" if self.files else "")
        if hasattr(self, "ntree"):
            self._render_noise()

    def _on_tree_click(self, event):
        """A click on the box at the left of a row shows or hides that trace."""
        iid = self.ftree.identify_row(event.y)
        if not iid or self.ftree.identify_column(event.x) != "#0":
            return
        bbox = self.ftree.bbox(iid, "#0")
        if not bbox or event.x - bbox[0] > 64:      # past the box and colour line: the name
            return
        f = self.files[int(iid)]
        f["show"] = not f["show"]
        self.ftree.item(iid, image=self._show_icon(f["color"], f["show"]))
        self._after_show_change()
        return "break"

    def _on_tree_double(self, event):
        iid = self.ftree.identify_row(event.y)
        bbox = self.ftree.bbox(iid, "#0") if iid else None
        if not bbox or self.ftree.identify_column(event.x) != "#0" or event.x - bbox[0] <= 64:
            return
        self.rename_selected(iid)
        return "break"

    def rename_selected(self, iid=None):
        """Edit a trace's run name in place. Enter saves, Esc cancels, an empty name restores the original."""
        iid = iid or (self.ftree.selection() or (None,))[0]
        if iid is None:
            messagebox.showinfo("Rename", "Select a trace in the list first.")
            return
        self.ftree.see(iid)
        self.ftree.update_idletasks()
        bbox = self.ftree.bbox(iid, "#0")
        if not bbox:
            return
        f = self.files[int(iid)]
        x, y, w, h = bbox
        e = ttk.Entry(self.ftree)
        e.insert(0, f.get("alias") or f["name"])
        e.select_range(0, tk.END)
        e.place(x=x + 44, y=y, width=max(w - 44, 80), height=h)
        e.focus_set()
        done = []

        def finish(save):
            if done:
                return
            done.append(1)
            name = e.get().strip()
            e.destroy()
            self.ftree.focus_set()
            if not save:
                return
            f["alias"] = name if name and name != f["name"] else None
            self._on_legend()

        e.bind("<Return>", lambda ev: finish(True))
        e.bind("<KP_Enter>", lambda ev: finish(True))
        e.bind("<Escape>", lambda ev: finish(False))
        e.bind("<FocusOut>", lambda ev: finish(True))

    def toggle_selected(self):
        for iid in self.ftree.selection():
            f = self.files[int(iid)]
            f["show"] = not f["show"]
        self.refresh_files()
        self._after_show_change()
        return "break"

    def set_all_shown(self, show):
        for f in self.files:
            f["show"] = show
        self.refresh_files()
        self._after_show_change()

    def _after_show_change(self):
        self._mark_noise_outdated()
        shown = sum(f["show"] for f in self.files)
        self.count_lbl.config(text=f"{shown} of {len(self.files)} shown" if self.files else "")
        self.draw()
        self._recalc_if_done(redraw=False)

    def _selected_files(self):
        return [self.files[int(i)] for i in self.ftree.selection() if int(i) < len(self.files)]

    def _select_file(self, f):
        idx = next((i for i, x in enumerate(self.files) if x is f), None)
        if idx is not None:
            self.ftree.selection_set(str(idx))
            self.ftree.see(str(idx))

    def _on_select(self):
        if self._noise_views:
            self.draw()                      # the threshold line follows the selected trace
        if self.tools:
            self.tools.refresh_readout()
        self.show_info()
        self._refresh_view()
        self._highlight_results()

    def _on_legend(self):
        self.refresh_files()
        self.show_info()
        self.draw()
        self._recalc_if_done(redraw=False)

    def _sync_legend_drag(self):
        """The legend can be dragged, except in cursors mode where a press would also grab a cursor."""
        if self._legend is not None:
            self._legend.set_draggable(getattr(self.tools, "mode", "select") != "cursors")

    def _on_legend_toggle(self):
        """Show or hide the legend box without redrawing the traces."""
        if not HAS_MPL:
            return
        if self._legend is None:
            if self.legend_on.get():
                self.draw()
            return
        self._legend.set_visible(self.legend_on.get())
        self.canvas.draw_idle()

    def remove_selected(self):
        idx = sorted((int(i) for i in self.ftree.selection()), reverse=True)
        if not idx:
            messagebox.showinfo("Remove", "Select one or more files in the list first.")
            return
        for i in idx:
            del self.files[i]
        self._auto_ctx = None
        self.noise_rows = [r for r in self.noise_rows if any(r[0] is f for f in self.files)]
        self._close_stale_noise_views()
        self.ftree.selection_set(())
        self.refresh_files()
        self.show_info()
        self.draw(reset_view=True)
        self._recalc_if_done(redraw=False)

    def clear_all(self):
        if self._noise_running:
            self.status.config(text="Noise analysis running — press Cancel first.")
            return
        self.files.clear()
        self._close_stale_noise_views()
        self.noise_rows = []
        self.noise_meta = None
        self.noise_outdated = False
        self._update_noise_banner()
        self._auto_ctx = None
        self.results = None
        self._result_files = []
        self.refresh_files()
        self._render_results()
        self.t0_var.set(""); self.t1_var.set("")
        self.show_info()
        self.draw(reset_view=True)

    def show_info(self):
        t = self.info
        t.config(state="normal")
        t.delete("1.0", tk.END)
        sel = self.ftree.selection()
        if not sel:
            t.insert(tk.END, "Select a trace to see its details." if self.files else "", "muted")
        else:
            f = self.files[int(sel[0])]
            meta = f["meta"]
            hide = {"title", "sheet_column", "run"}
            if "recipe" in meta:                     # the raw recipe line is shown; skip the parts
                hide |= {"recipe_name", "step", "channel"}
            t.insert(tk.END, f"Details · {self._label(f)}\n", "head")
            rows = [("File", f["file"]), ("Folder", f["folder"])]
            if f.get("alias"):
                rows.insert(0, ("Original name", f["name"]))
            if "sheet_column" in meta:
                rows.append(("Column", meta["sheet_column"]))
            rows += [(PRETTY.get(k, k), v) for k, v in meta.items() if k not in hide]
            self._info_rows(rows)
            t.insert(tk.END, "File footer (whole run)\n", "sub")
            self._info_rows([(PRETTY.get(k, k), v) for k, v in f["summary"].items()] or [("(none)", "")])
            t.insert(tk.END, "\n" + f["path"], "muted")
        t.config(state="disabled")

    def _info_rows(self, rows):
        for k, v in rows:
            self.info.insert(tk.END, f"{k}\t", ("key", "line"))
            self.info.insert(tk.END, f"{v}\n", "line")

    # ---------- window ----------
    def _time_extent(self, files=None):
        files = self.files if files is None else files
        if not files:
            return None
        return min(f["data"].t_start for f in files), max(f["data"].t_end for f in files)

    def full_range(self, calc=True):
        ext = self._time_extent()
        if ext is None:
            return
        self.t0_var.set(f"{ext[0]:g}")
        self.t1_var.set(f"{ext[1]:g}")
        if calc:
            self.calculate()
        else:
            self.draw()

    def _get_range(self, quiet=False):
        try:
            t0 = float(self.t0_var.get()); t1 = float(self.t1_var.get())
            if not (math.isfinite(t0) and math.isfinite(t1)):
                raise ValueError
        except ValueError:
            if not quiet:
                messagebox.showwarning("Window", "Enter start and end times as numbers of seconds, e.g. 1 or 2.5.")
            return None
        if t1 < t0:
            if not quiet:
                messagebox.showwarning("Window", "The end time is before the start time.")
            return None
        return t0, t1

    def _recalc_if_done(self, redraw=True):
        """Recalculate if results are showing. redraw=False when the caller has just drawn the chart."""
        if self.results is not None and self.files:
            self.calculate(redraw=redraw)

    def _stat_files(self):
        return [f for f in self.files if f["show"] or not self.visible_only.get()]

    # ---------- stats ----------
    def calculate(self, redraw=True):
        if not self.files:
            messagebox.showinfo("No data", "Add one or more CSV or Excel files first.")
            return
        rng = self._get_range()
        if rng is None:
            return
        t0, t1 = rng
        pop = not self.sample_var.get()
        rows, files = [], self._stat_files()
        for f in files:
            s = f["data"].stats(t0, t1, population=pop)
            s.pop("RMS", None)               # not shown or exported yet
            rows.append({
                "Label": self._label(f),
                "Folder": f["folder"], "File": f["file"],
                "Run": field(f, "run"), "Experiment": field(f, "experiment"),
                "Sample rate (Hz)": field(f, "sample_rate_hz"),
                "LED": field(f, "led"),
                "PMT gain (mV)": field(f, "pmt_gain_mv"),
                "Concentration (nM)": field(f, "concentration_nm"),
                "Position (mm)": field(f, "position_mm"),
                "Recipe": field(f, "recipe_name"), "Step": field(f, "step"), "Channel": field(f, "channel"),
                **s, "color": f["color"]})
        self.results = pd.DataFrame(rows)
        self._result_files = files
        self.result_meta = (t0, t1, pop)
        self._render_results()

        empty = [r["Label"] for r in rows if r["Points"] == 0]
        msg = f"Calculated statistics for {len(rows)} file(s)."
        if empty:
            msg += f" No points in the window for: {', '.join(empty)}."
        self.status.config(text=msg)
        if redraw:
            self.draw()

    RES_ROW_H = 28
    RES_HEAD_H = 26
    RES_BAR_W = 220

    def _render_results(self):
        """The statistics table: one row per trace, with a min-to-max bar on a shared scale."""
        cv = self.rcanvas
        cv.delete("all")
        cv.yview_moveto(0)
        self._res_rows = []
        rows = [] if self.results is None else self.results.to_dict("records")
        if not rows:
            self.range_lbl.config(text="Statistics")
            self.range_sub.config(text="")
            msg = ("Set a time window and press Calculate, or drag across the chart."
                   if self.results is None else "No traces to calculate. Show a trace or untick "
                                                "'Shown files only'.")
            cv.create_text(0, 14, text=msg, anchor="w", fill=MUTED, font=self._font)
            cv.configure(scrollregion=(0, 0, 1, 1))
            return

        t0, t1, pop = self.result_meta
        pts = {int(r["Points"]) for r in rows}
        self.range_lbl.config(text=f"Window {t0:g} – {t1:g} s")
        self.range_sub.config(text=f"{fmt(pts.pop()) + ' points per trace' if len(pts) == 1 else 'Points vary by trace'}"
                                   f" · {'population' if pop else 'sample'} std · every point used · values in mV")

        has = [r for r in rows if r["Points"]]
        lo = min((r["Min"] for r in has), default=0.0)
        hi = max((r["Max"] for r in has), default=1.0)
        pad = (hi - lo) * 0.03 or 1.0
        lo, hi = lo - pad, hi + pad

        # column widths from the widest text in each column, 8 px padding each side
        nums = [[fmt(int(r["Points"])), fmt(r["Mean"]), fmt(r["Std"]), fmt(r["Max"]), fmt(r["Min"]),
                 fmt(r["Time of max"])] for r in rows]
        head_w = [self._small_bold.measure(h) for h in self.RES_HEAD]
        widths = [max(head_w[0], 18 + max(self._bold.measure(r["Label"]) for r in rows)) + 16,
                  max(head_w[1], self.RES_BAR_W) + 16]
        for k in range(len(nums[0])):
            font = self._mono_bold if k == 1 else self._mono
            widths.append(max(head_w[k + 2], max(font.measure(n[k]) for n in nums)) + 16)
        lefts = [sum(widths[:c]) for c in range(len(widths))]
        total_w = sum(widths)

        rh, top, far = self.RES_ROW_H, self.RES_HEAD_H, 10000      # far: rows reach past any window width
        for c, h in enumerate(self.RES_HEAD):
            x, anchor = (lefts[c] + 8, "w") if c < 2 else (lefts[c] + widths[c] - 8, "e")
            cv.create_text(x, top / 2 - 2, text=h, anchor=anchor, fill=MUTED, font=self._small_bold)
        cv.create_line(0, top, far, top, fill=BORDER)

        selected = {id(f) for f in self._selected_files()}
        for i, (r, f, n) in enumerate(zip(rows, self._result_files, nums)):
            sel = id(f) in selected
            y0 = top + 1 + i * rh
            ym = y0 + rh / 2
            cv.create_rectangle(0, y0, far, y0 + rh, fill=SEL_BG if sel else CARD, outline="", tags=f"bg{i}")
            cv.create_text(lefts[0] + 8, ym, text="●", anchor="w", fill=r["color"], font=self._font)
            cv.create_text(lefts[0] + 26, ym, text=r["Label"], anchor="w", fill=TEXT,
                           font=self._bold if sel else self._font, tags=f"name{i}")
            self._draw_range(cv, r, lo, hi, lefts[1] + 8, ym)
            for k, v in enumerate(n):
                c = k + 2
                cv.create_text(lefts[c] + widths[c] - 8, ym, text=v, anchor="e", fill=TEXT,
                               font=self._mono_bold if k == 1 else self._mono)
            cv.create_line(0, y0 + rh, far, y0 + rh, fill="#edf0f3")
            self._res_rows.append((f, sel))
        cv.configure(scrollregion=(0, 0, total_w, top + 1 + len(rows) * rh + 1))

    def _on_res_click(self, event):
        i = int((self.rcanvas.canvasy(event.y) - self.RES_HEAD_H - 1) // self.RES_ROW_H)
        if 0 <= i < len(self._res_rows):
            self._select_file(self._res_rows[i][0])

    def _highlight_results(self):
        """Selection changed: recolour only the rows whose selected state changed (no rebuild)."""
        selected = {id(f) for f in self._selected_files()}
        for i, (f, was) in enumerate(self._res_rows):
            sel = id(f) in selected
            if sel != was:
                self.rcanvas.itemconfigure(f"bg{i}", fill=SEL_BG if sel else CARD)
                self.rcanvas.itemconfigure(f"name{i}", font=self._bold if sel else self._font)
                self._res_rows[i] = (f, sel)

    def _draw_range(self, cv, r, lo, hi, x0, ym):
        w, h = self.RES_BAR_W, 18
        x = lambda v: x0 + 4 + (v - lo) / (hi - lo) * (w - 8)
        cv.create_line(x0 + 4, ym, x0 + w - 4, ym, fill=BORDER, width=2)
        if not r["Points"]:
            cv.create_text(x0 + 4, ym, text="no points in the window", anchor="w", fill=MUTED, font=self._small)
            return
        cv.create_rectangle(x(r["Min"]), ym - h / 2 + 4, max(x(r["Max"]), x(r["Min"]) + 2), ym + h / 2 - 4,
                            fill=_tint(r["color"], 0.65), outline="")
        m = x(r["Mean"])
        cv.create_oval(m - 4, ym - 4, m + 4, ym + 4, fill=r["color"], outline="")


    # ---------- noise analysis ----------
    NOISE_OUTDATED = "Window or settings changed — press Analyze noise to update."

    def _mark_noise_outdated(self):
        self.noise_outdated = True
        if hasattr(self, "noise_banner"):
            self._update_noise_banner()

    def _update_noise_banner(self):
        show = self.noise_outdated and self.noise_meta is not None
        self.noise_banner.config(text=self.NOISE_OUTDATED if show else "")
        if show:
            self.noise_banner.pack(fill=tk.X, before=self._noise_body)
        else:
            self.noise_banner.pack_forget()

    def _set_noise_busy(self, busy):
        self._noise_running = busy
        state = "disabled" if busy else "normal"
        for b in (self.noise_btn, self.calc_btn, self.add_btn, self.clear_btn):
            b.config(state=state)
        self._file_menu.entryconfigure("Clear all", state=state)
        self.config(cursor="watch" if busy else "")

    def analyze_noise(self):
        """Noise statistics of every trace Calculate would use, on a background thread."""
        if self._loading or self._noise_running:
            return
        if not self.files:
            messagebox.showinfo("No data", "Add one or more CSV or Excel files first.")
            return
        rng = self._get_range()
        if rng is None:
            return
        files = self._stat_files()
        if not files:
            messagebox.showinfo("Analyze noise", "No traces to analyze. Show a trace or untick 'Shown files only'.")
            return
        t0, t1 = rng
        k, f_min, watch = self.settings["spike_k"], self.settings["f_min_hz"], tuple(self.settings["watch_hz"])
        self.noise_rows = []
        self.noise_meta = (t0, t1, k, f_min, watch, None)
        self.noise_outdated = False
        self._update_noise_banner()
        self._noise_n = len(files)
        self._noise_files = list(files)
        self._noise_cancel.clear()
        self._set_noise_busy(True)
        self.progress.config(maximum=len(files), value=0)
        self.progress.pack(side=tk.RIGHT, padx=(0, 16))
        self.cancel_btn.pack(side=tk.RIGHT)
        q = self._noise_q = queue.Queue()
        cancel = self._noise_cancel

        def work():                       # never touches Tk: results go through the queue
            for i, f in enumerate(files):
                if cancel.is_set():
                    break
                q.put(("progress", (i, len(files), f["file"])))
                try:
                    res = noise.analyze(f["data"], t0, t1, list(watch), k, f_min)
                except Exception as e:
                    res = e
                q.put(("row", (f, res)))
            q.put(("done", None))

        threading.Thread(target=work, daemon=True).start()
        self.after(50, self._poll_noise)

    def cancel_noise(self):
        """Stop after the trace in progress; rows done so far are kept."""
        self._noise_cancel.set()

    def _poll_noise(self):
        done = False
        try:
            while True:
                kind, val = self._noise_q.get_nowait()
                if kind == "progress":
                    i, n, name = val
                    self.progress.config(value=i)
                    self.status.config(text=f"Analyzing noise {i + 1}/{n} · {name}")
                elif kind == "row":
                    if self.noise_meta is not None:
                        self.noise_rows.append(val)
                elif kind == "done":
                    done = True
        except queue.Empty:
            pass
        if not done:
            self.after(50, self._poll_noise)
            return
        self.progress.pack_forget()
        self.cancel_btn.pack_forget()
        self._set_noise_busy(False)
        if self.noise_meta is None:        # cleared meanwhile
            self._render_noise()
            return
        t0, t1, k, f_min, watch, _ = self.noise_meta
        self.noise_rows = [r for r in self.noise_rows if any(r[0] is f for f in self.files)]
        res = next((r.resolution_hz for _, r in self.noise_rows
                    if not isinstance(r, Exception) and r.resolution_hz), None)
        self.noise_meta = (t0, t1, k, f_min, watch, res)
        n_total = sum(1 for f in self._noise_files if any(f is g for g in self.files))
        n_done, errors = len(self.noise_rows), sum(isinstance(r, Exception) for _, r in self.noise_rows)
        if n_done < n_total:
            msg = f"Noise analysis cancelled ({n_done} of {n_total} done)."
        else:
            msg = f"Analyzed noise for {n_done} trace(s)."
        if errors:
            msg += f" {errors} failed (see 'error' in the Noise tab)."
        self.status.config(text=msg)
        self._update_noise_banner()
        self._render_noise()
        self.results_nb.select(self.noise_tab)

    def _noise_columns(self):
        watch = self.noise_meta[4] if self.noise_meta else ()
        cols = ["Trace", "Baseline (mV)", "Noise σ (mV)", "Threshold (mV)", "Spikes", "Rate (/s)",
                "Median spacing (s)", "Spacing rate (Hz)"]
        for i in (1, 2, 3):
            cols += [f"Peak {i} (Hz)", f"Peak {i} (mV RMS)"]
        return cols + [f"{f:.10g} Hz (mV RMS)" for f in watch]

    def _noise_cells(self):
        """One list of values per row, in the current trace order; None = no value."""
        watch = self.noise_meta[4] if self.noise_meta else ()
        n_cells = len(self._noise_columns()) - 1
        by_id = {id(f): r for f, r in self.noise_rows}
        out = []
        for f in self.files:
            r = by_id.get(id(f))
            if r is None:
                continue
            if isinstance(r, Exception):
                cells = ["–"] * n_cells
                cells[3] = "error"
            elif r.note == noise.NOTE_EMPTY:
                cells = ["–"] * n_cells
            else:
                peaks = list(r.peaks[:3]) + [(None, None)] * (3 - len(r.peaks[:3]))
                cells = [r.baseline, r.sigma, r.threshold, r.n_spikes, r.rate, r.median_spacing, r.spacing_hz]
                for hz, mv in peaks:
                    cells += [hz, mv]
                cells += [r.watch.get(float(w)) for w in watch]
            label = self._label(f)
            run_k = self.noise_meta[2] if self.noise_meta else None
            if not isinstance(r, Exception) and run_k is not None and r.k != run_k:
                label += f" (k={r.k:g})"
            out.append([label] + cells)
        return out

    def noise_table(self, for_export=False):
        """The Noise tab as a DataFrame, never re-analysing; for_export adds the settings used."""
        df = pd.DataFrame(self._noise_cells(), columns=self._noise_columns())
        if for_export and self.noise_meta:
            t0, t1, k, f_min, _, res = self.noise_meta
            df["Window start (s)"] = t0
            df["Window end (s)"] = t1
            by_id = {id(f): r for f, r in self.noise_rows}
            ks = [k if isinstance(by_id[id(f)], Exception) else by_id[id(f)].k
                  for f in self.files if id(f) in by_id]
            df["k"] = ks
            df["f_min (Hz)"] = f_min
            df["Resolution (Hz)"] = res
        return df

    def _render_noise(self):
        tree = self.ntree
        tree.delete(*tree.get_children())
        cols = self._noise_columns()
        tree["columns"] = cols
        for c in cols:
            first = c == "Trace"
            tree.heading(c, text=c, anchor="w" if first else "e")
            tree.column(c, width=220 if first else max(96, self._small_bold.measure(c) + 28),
                        anchor="w" if first else "e", stretch=False)
        for cells in self._noise_cells():
            tree.insert("", tk.END, values=[v if isinstance(v, str) else fmt(v) for v in cells])
        if self.noise_meta:
            t0, t1, k, f_min, _, res = self.noise_meta
            sub = f"Window {t0:g} – {t1:g} s · spike k {k:g} · lowest frequency {f_min:g} Hz"
            if res:
                sub += f" · resolution {res:.3g} Hz"
        else:
            sub = "Set a time window and press Analyze noise."
        self.noise_sub.config(text=sub)

    def _copy_noise(self):
        if not self.noise_rows:
            messagebox.showinfo("Nothing to export", "Analyze noise first.")
            return
        text = self.noise_table().to_csv(sep="\t", index=False, lineterminator="\n").rstrip("\n")
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.config(text="Noise results copied. Paste into Excel.")

    def _export_noise(self):
        if not self.noise_rows:
            messagebox.showinfo("Nothing to export", "Analyze noise first.")
            return
        out = self.noise_table(for_export=True)
        path = filedialog.asksaveasfilename(
            title="Export results", defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("Excel workbook", "*.xlsx")],
            initialfile="noise_analysis.csv")
        if not path:
            return
        try:
            if path.lower().endswith(".xlsx"):
                out.to_excel(path, index=False, sheet_name="Noise")
            else:
                out.to_csv(path, index=False)
            self.status.config(text=f"Exported noise results to {path}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    # ---------- noise view ----------
    def open_noise_view(self, trace):
        """The noise view of one trace; an already open one is raised instead of duplicated."""
        if not HAS_MPL:
            return None
        view = self._noise_views.get(id(trace))
        if view is not None:
            view.deiconify()
            view.lift()
            return view
        view = self._noise_views[id(trace)] = NoiseView(self, trace)
        return view

    def open_selected_noise_view(self):
        sel = self._selected_files()
        f = sel[0] if sel else next((x for x in self.files if x["show"]), None)
        if f is None:
            messagebox.showinfo("Noise view", "Add a trace first.")
            return
        self.open_noise_view(f)

    # ---------- smoothing ----------
    def open_smoothing(self, f=None):
        """Open a rolling-average tab (smoothing_function.py) for one trace in the results notebook.

        Uses the trace selected in the file list, else the first shown one. Start x defaults to
        the start of the current window. "Send to main chart" adds the result as a new trace."""
        if f is None:
            sel = self._selected_files()
            f = sel[0] if sel else next((x for x in self.files if x["show"]), None)
        if f is None:
            messagebox.showinfo("Smoothing", "Add a trace first.")
            return None
        d = f["data"]
        rng = self._get_range(quiet=True)
        self.config(cursor="watch"); self.update_idletasks()
        try:
            t = np.array(d.times(0, d.n), dtype=float)
            mv = np.array(d.values(0, d.n), dtype=float)
            return open_smoothing_tab(
                self.results_nb, t, mv, name=self._label(f),
                x_label=f.get("t_label", "Time (s)"), y_label=f.get("v_label", "Signal (mV)"),
                start_x=rng[0] if rng else None,
                on_apply=lambda x, s, p: self._add_smoothed_trace(f, x, np.where(np.isnan(s), mv, s), p))
        finally:
            self.config(cursor="")

    def _add_smoothed_trace(self, src, t, mv, params):
        """Add a smoothed copy of src as a new in-memory trace (points before start x are raw)."""
        if self._loading or self._noise_running:
            self.status.config(text="Wait for loading or noise analysis to finish, then send it again.")
            return None
        window, start_x = params["window"], params.get("start_x")
        desc = f"{window}-point {'centered' if params.get('center', True) else 'trailing'} rolling average"
        if start_x is not None:
            desc += f" from {start_x:g} s"
        self.config(cursor="watch"); self.update_idletasks()
        try:
            data = TraceData.from_arrays(t, mv)
        finally:
            self.config(cursor="")
        fields = {k: v for k, v in src.items()
                  if k not in ("data", "n", "show", "color", "load_idx", "alias")}
        fields["meta"] = dict(src["meta"], smoothing=desc)
        new = dict(fields, data=data, n=data.n, show=True, color=None, load_idx=self._load_seq,
                   alias=f"{src.get('alias') or src['name']} (smoothed {window} pts)")
        self._load_seq += 1
        self.files.append(new)
        new["color"] = self._next_color()
        self._auto_ctx = None
        self._mark_noise_outdated()
        self.files[:] = sorted_files(self.files, self.sort_var.get())
        self.refresh_files()
        self._select_file(new)
        self.draw()
        self._recalc_if_done(redraw=False)
        self.status.config(text=f"Added {new['alias']}: {desc}.")
        return new

    def _on_noise_double(self, event):
        row = self.ntree.identify_row(event.y)
        if not row:
            return
        by_id = {id(f) for f, _ in self.noise_rows}
        traces = [f for f in self.files if id(f) in by_id]
        i = self.ntree.index(row)
        if i < len(traces):
            self.open_noise_view(traces[i])

    def _redraw_threshold(self):
        if any(id(v.trace) in {id(f) for f in self._selected_files()} for v in self._noise_views.values()):
            self.draw()

    def _noise_view_closed(self, view):
        if self._noise_views.get(id(view.trace)) is view:
            del self._noise_views[id(view.trace)]
        try:
            if any(f is view.trace for f in self._selected_files()):
                self.draw()
        except tk.TclError:
            pass                              # the whole app is closing

    def _close_stale_noise_views(self):
        for v in list(self._noise_views.values()):
            if not any(f is v.trace for f in self.files):
                v.close()

    def _update_noise_row(self, trace, sp, window, k):
        """Bring the trace's Noise row up to date with a spike re-run over the same window."""
        if self.noise_meta is None or tuple(self.noise_meta[:2]) != tuple(window):
            return
        for i, (f, r) in enumerate(self.noise_rows):
            if f is trace and not isinstance(r, Exception):
                n = len(sp.times)
                spacing = float(np.median(np.diff(sp.times))) if n >= 2 else None
                self.noise_rows[i] = (f, dataclasses.replace(
                    r, k=k, baseline=sp.baseline, sigma=sp.sigma, threshold=sp.threshold, n_spikes=n,
                    rate=n / sp.duration if sp.duration > 0 else 0.0, median_spacing=spacing,
                    spacing_hz=1.0 / spacing if spacing else None, spikes=sp))
                self._render_noise()
                return

    def add_watch_frequency(self, freq):
        """Add a watch-list frequency (0.1 Hz resolution), save it and mark results outdated."""
        hz = round(float(freq), 1)
        watch = list(self.settings["watch_hz"])
        if hz in watch:
            return
        self.settings = {**self.settings, "watch_hz": watch + [hz]}
        try:
            save_settings(self.settings_path, self.settings)
        except OSError as e:
            self.status.config(text=f"Couldn't save the watch-list ({e}); it applies for this session only.")
        self._mark_noise_outdated()
        for v in self._noise_views.values():
            if v.ready:
                v._draw_watch()

    # ---------- noise settings ----------
    def _apply_noise_settings(self, watch_text, k_text, f_min_text):
        """Validate, store and save; returns the error messages (empty on success)."""
        new, errors = parse_settings(watch_text, k_text, f_min_text)
        if errors:
            return errors
        changed = new != self.settings
        self.settings = new
        try:
            save_settings(self.settings_path, new)
        except OSError as e:
            self.status.config(text=f"Couldn't save noise settings ({e}); they apply for this session only.")
        if changed:
            self._mark_noise_outdated()
        return []

    def show_noise_settings(self):
        if self._nset and self._nset["win"].winfo_exists():
            self._nset["win"].lift()
            return
        s = self.settings
        win = tk.Toplevel(self)
        win.title("Noise settings")
        win.configure(bg=CARD)
        win.transient(self)
        win.resizable(False, False)
        body = ttk.Frame(win, style="Card.TFrame", padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        vars_ = {"watch": tk.StringVar(value=", ".join(f"{x:.10g}" for x in s["watch_hz"])),
                 "k": tk.StringVar(value=f"{s['spike_k']:g}"),
                 "fmin": tk.StringVar(value=f"{s['f_min_hz']:g}")}
        first = None
        for r, (label, key) in enumerate((("Watch-list (Hz)", "watch"), ("Spike k", "k"),
                                          ("Lowest frequency (Hz)", "fmin"))):
            ttk.Label(body, text=label, style="Card.TLabel").grid(row=r, column=0, sticky="w", pady=4)
            e = ttk.Entry(body, textvariable=vars_[key], width=30)
            e.grid(row=r, column=1, sticky="ew", padx=(12, 0), pady=4)
            first = first or e
        ttk.Label(body, text="Watch-list: frequencies separated by commas or spaces. Spike k: spikes are "
                             "peaks more than k noise σ above the baseline.", style="Muted.Card.TLabel",
                  wraplength=380).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        err = ttk.Label(body, text="", style="Card.TLabel", foreground="#a33", wraplength=380)
        err.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        btns = ttk.Frame(body, style="Card.TFrame")
        btns.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))

        def close(_e=None):
            try:
                win.grab_release()
            except tk.TclError:
                pass
            win.destroy()
            self._nset = None

        def save(_e=None):
            errors = self._apply_noise_settings(vars_["watch"].get(), vars_["k"].get(), vars_["fmin"].get())
            if errors:
                err.config(text="\n".join(errors))
            else:
                close()
        ttk.Button(btns, text="Cancel", command=close).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Save", style="Accent.TButton", command=save).pack(side=tk.RIGHT, padx=(0, 8))
        win.bind("<Return>", save)
        win.bind("<Escape>", close)
        win.protocol("WM_DELETE_WINDOW", close)
        self._nset = dict(win=win, err=err, save=save, **vars_)
        try:
            win.grab_set()
        except tk.TclError:
            pass
        first.focus_set()

    # ---------- export ----------
    def _results_table(self):
        if self.results is None or self.results.empty:
            messagebox.showinfo("Nothing to export", "Calculate statistics first.")
            return None
        t0, t1, pop = self.result_meta
        out = self.results.drop(columns=["color"]).copy()
        out["Window start (s)"] = t0
        out["Window end (s)"] = t1
        out["Std. dev. type"] = "population" if pop else "sample"
        return out

    def _noise_showing(self):
        return self.results_nb.select() == str(self.noise_tab)

    def export_results(self):
        if self._noise_showing():
            return self._export_noise()
        out = self._results_table()
        if out is None:
            return
        path = filedialog.asksaveasfilename(
            title="Export results", defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("Excel workbook", "*.xlsx")],
            initialfile="window_statistics.csv")
        if not path:
            return
        try:
            if path.lower().endswith(".xlsx"):
                out.to_excel(path, index=False, sheet_name="Statistics")
            else:
                out.to_csv(path, index=False)
            self.status.config(text=f"Exported results to {path}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    def copy_results(self):
        if self._noise_showing():
            return self._copy_noise()
        out = self._results_table()
        if out is None:
            return
        cols = ["Label", "Run", "PMT gain (mV)", "Concentration (nM)", "Channel",
                "Points", "Max", "Min", "Mean", "Std"]
        self.clipboard_clear()
        self.clipboard_append(out[cols].to_csv(sep="\t", index=False, lineterminator="\n").rstrip("\n"))
        self.status.config(text="Results copied. Paste into Excel.")

    def _window_data(self, files, t0, t1):
        """(DataFrame with one Time/mV column pair per file, number of points) for the window."""
        parts, total = [], 0
        for f in files:
            d = f["data"]
            i0, i1 = d.index_range(t0, t1)
            total += i1 - i0
            lab = self._label(f)
            parts.append(pd.DataFrame({f"{lab} time_s": d.times(i0, i1), f"{lab} mV": d.values(i0, i1)}))
        return pd.concat(parts, axis=1), total

    def export_window_data(self):
        """Every point inside the window, one Time/mV column pair per file."""
        files = self._stat_files()
        if not files:
            messagebox.showinfo("Nothing to export", "Add and show at least one file.")
            return
        rng = self._get_range()
        if rng is None:
            return
        t0, t1 = rng
        out, total = self._window_data(files, t0, t1)
        if total > 1_000_000 and not messagebox.askyesno(
                "Large export", f"The window holds {total:,} points. Exporting may take a while "
                                "and Excel can only open 1,048,576 rows. Continue?"):
            return
        path = filedialog.asksaveasfilename(
            title="Export window data", defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("Excel workbook", "*.xlsx")],
            initialfile=f"window_{t0:g}s_to_{t1:g}s.csv")
        if not path:
            return
        try:
            self.config(cursor="watch"); self.update_idletasks()
            out.to_excel(path, index=False) if path.lower().endswith(".xlsx") else out.to_csv(path, index=False)
            self.status.config(text=f"Exported window data for {len(files)} file(s) to {path}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))
        finally:
            self.config(cursor="")

    # ---------- chart ----------
    def _empty_chart(self, msg):
        if self.tools:
            self.tools.detach()          # no live drag selector or stale history on the placeholder
        self.ax.clear()
        self._lines = []
        self._legend = None
        for s in self.ax.spines.values():
            s.set_visible(False)
        self.ax.text(0.5, 0.5, msg, ha="center", va="center", color=MUTED, transform=self.ax.transAxes)
        self.ax.set_xticks([]); self.ax.set_yticks([])
        self.view_lbl.config(text="")
        self.canvas.draw_idle()

    def _label(self, f):
        mode = self.legend_var.get()
        run = f.get("alias") or f["name"]
        if f.get("alias") and mode in ("Auto", "Folder / file"):   # a name the user chose wins
            return run
        if mode == "Auto":
            if self._auto_ctx is None:              # once per change of the file list, not per label
                self._auto_ctx = (len({field(x, "experiment") for x in self.files}) > 1,
                                  len({x["folder"] for x in self.files}) > 1)
            many_exps, many_folders = self._auto_ctx
            if many_exps:
                return f"{run} · {field(f, 'experiment')}"
            if many_folders:
                return f"{f['folder']}/{f['file']}"
            return run
        if mode == "Run + experiment":
            return f"{run} · {field(f, 'experiment')}"
        if mode == "Folder / file":
            return f"{f['folder']}/{f['file']}"
        if mode == "PMT gain":
            return f"{run} · {field(f, 'pmt_gain_mv')} mV gain"
        if mode == "Concentration":
            return f"{run} · {field(f, 'concentration_nm')} nM"
        if mode == "Channel":
            return f"{run} · {field(f, 'channel') or 'no channel'}"
        if mode == "Position":
            return f"{run} · {field(f, 'position_mm')} mm"
        return run

    def _apply_style(self, line, raw, selected=False):
        """Every point shown: the chosen style. Zoomed out (min/max per slice): a solid
        zig-zag line, which reads as the min-to-max band, like an oscilloscope.
        The trace selected in the file list is drawn thicker and on top."""
        style = self.style_var.get()
        if not raw:
            line.set(ls="-", lw=0.6, marker="None", alpha=0.45)
        elif style == "Line":
            line.set(ls="-", lw=1.0, marker="None", alpha=1.0)
        elif style == "Points + line":
            line.set(ls="-", lw=0.7, marker="o", ms=2.5, alpha=1.0)
        else:
            line.set(ls="None", marker="o", ms=2.5, alpha=0.8)
        if selected:
            line.set(lw=line.get_linewidth() * 2.2, ms=line.get_markersize() * 1.6,
                     alpha=min(1.0, line.get_alpha() + 0.35))
        line.set_zorder(3 if selected else 2)

    def draw(self, reset_view=False):
        if not HAS_MPL:
            return
        if reset_view and self.tools:
            self.tools.clear_history()
        shown = [f for f in self.files if f["show"]]
        if not shown:
            self._empty_chart("Tick the box next to a trace to see its data." if self.files
                              else "Add one or more CSV or Excel files to see the data.")
            return
        ax = self.ax
        keep = bool(self._lines) and not reset_view
        if keep:
            xl, yl = ax.get_xlim(), ax.get_ylim()
            x0, x1 = xl
        else:
            x0, x1 = self._time_extent(shown)
        ax.clear()
        ax.set_facecolor(CARD)
        self._lines = []
        self._legend = None
        sel = {id(f) for f in self._selected_files()}
        budget = self._draw_budget(len(shown))
        for f in shown:
            t, y, raw = f["data"].view(x0, x1, *budget)
            line, = ax.plot(t, y, color=f["color"], label=self._label(f))
            self._apply_style(line, raw, id(f) in sel)
            self._lines.append((line, f))
        ax.set_xlabel(shown[0]["t_label"], color=MUTED); ax.set_ylabel(shown[0]["v_label"], color=MUTED)
        ax.grid(True, axis="y", color="#e7ebef", lw=0.8)
        ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set(visible=True, color="#9aa5b1")
        ax.tick_params(colors=MUTED, labelsize=9)
        if self.legend_on.get():
            leg = ax.legend(loc="best" if len(shown) <= BEST_LEGEND_MAX else "upper right",
                            fontsize=8, ncol=1 + (len(shown) - 1) // 8,
                            labelspacing=0.3, handlelength=1.2, handletextpad=0.4, borderpad=0.4,
                            columnspacing=1.0, facecolor=CARD, edgecolor=BORDER, framealpha=0.9)
            leg.set_in_layout(False)       # else tight_layout squeezes the axes to make room for it
            self._legend = leg
            self._sync_legend_drag()
            for h in leg.legend_handles:
                h.set(lw=2, alpha=1.0, ms=4)                   # same size whichever trace is selected
        rng = self._get_range(quiet=True)
        if rng:
            # grey out everything outside the window, dashed lines at its edges
            t0, t1 = rng
            lo, hi = self._time_extent()
            far = (hi - lo) or 1.0
            ax.axvspan(min(lo, t0) - far, t0, color=SHADE, alpha=0.85, lw=0, zorder=0)
            ax.axvspan(t1, max(hi, t1) + far, color=SHADE, alpha=0.85, lw=0, zorder=0)
            ax.axvline(t0, color=ACCENT, lw=1.2, ls=(0, (4, 3)))
            ax.axvline(t1, color=ACCENT, lw=1.2, ls=(0, (4, 3)))
        for v in self._noise_views.values():
            thr = v.spikes.threshold if v.spikes is not None else math.nan
            if id(v.trace) in sel and math.isfinite(thr):
                ax.axhline(thr, color="#d92d20", lw=1.2, ls=":")
        if keep:
            ax.set_xlim(xl); ax.set_ylim(yl)
        else:
            ax.set_xlim(x0, x1)
        ax.callbacks.connect("xlim_changed", self._on_xlim)   # ax.clear() drops old callbacks
        self._relayout()
        self.tools.attach()                  # ax.clear() removed the drag selector
        self._update_view_label()
        self.canvas.draw_idle()

    def reset_zoom(self):
        if self.tools:
            self.tools.reset()
        else:
            self.draw(reset_view=True)

    def _autoscale_y(self, x0, x1):
        """(ymin, ymax) of the plotted points in [x0, x1] plus 3 % padding, or None.

        Computed fresh from TraceData.view() for the new range (the same min/max-decimated
        points the chart will draw once its debounced re-decimation runs), not from the
        lines' current, possibly stale, data, so spikes are never clipped."""
        budget = self._draw_budget(len(self._lines))
        lo = hi = None
        for _, f in self._lines:
            t, y, _raw = f["data"].view(x0, x1, *budget)
            if len(t) == 0:
                continue
            keep = (t >= x0) & (t <= x1)
            if keep.any():
                t, y = t[keep], y[keep]
            lo = float(np.min(y)) if lo is None else min(lo, float(np.min(y)))
            hi = float(np.max(y)) if hi is None else max(hi, float(np.max(y)))
        if lo is None:
            return None
        pad = (hi - lo) * 0.03 or abs(hi) * 0.03 or 1.0
        return lo - pad, hi + pad

    def _relayout(self, _event=None):
        """Fit the axes to the current canvas size (also on every window resize)."""
        try:
            self.fig.tight_layout()
        except Exception:
            pass

    def _on_xlim(self, _ax):
        """Zoom/pan changed: re-pick which points to draw (debounced)."""
        if self._xlim_job is not None:
            self.after_cancel(self._xlim_job)
        self._xlim_job = self.after(60, self._refresh_view)

    def _refresh_view(self):
        self._xlim_job = None
        if not HAS_MPL or not self._lines:
            return
        x0, x1 = self.ax.get_xlim()
        sel = {id(f) for f in self._selected_files()}
        budget = self._draw_budget(len(self._lines))
        for line, f in self._lines:
            t, y, raw = f["data"].view(x0, x1, *budget)
            line.set_data(t, y)
            self._apply_style(line, raw, id(f) in sel)
        self._update_view_label()
        self.canvas.draw_idle()

    def _draw_budget(self, n_traces):
        return draw_budget(n_traces, self.canvas.get_tk_widget().winfo_width())

    def _update_view_label(self):
        x0, x1 = self.ax.get_xlim()
        raw_limit = self._draw_budget(len(self._lines))[0]
        reduced = []
        for _, f in self._lines:
            i0, i1 = f["data"].index_range(x0, x1)
            if i1 - i0 > raw_limit:
                reduced.append(f"{self._label(f)} ({i1 - i0:,} pts)")
        if reduced:
            which = (", ".join(reduced) if len(reduced) <= 4
                     else f"{len(reduced)} of {len(self._lines)} traces")
            self.view_lbl.config(
                text="Zoomed out: drawing the min-to-max band (every spike kept) for " + which
                     + ". Zoom in (Zoom ↔ tool) to see every point. Statistics always use every point.",
                foreground="#a06000")
        else:
            self.view_lbl.config(text="Showing every data point.", foreground=MUTED)

    def _on_span(self, xmin, xmax):
        if xmax - xmin <= 0:
            return
        self.t0_var.set(f"{xmin:.6g}")
        self.t1_var.set(f"{xmax:.6g}")
        self.after(10, self.calculate)


if __name__ == "__main__":
    App().mainloop()
