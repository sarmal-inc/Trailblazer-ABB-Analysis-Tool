"""Intensity histograms and control comparison (numpy only, no Tk).

Plot A bins every sample: one bin per DAQ output level within +-LIN_MV of the reference (so codes
the DAQ never outputs show as empty bins), PER_DECADE log bins beyond. Plot B bins spike peaks in
log bins from the threshold up. compare() groups rows by PMT gain and sets positive rows against
the negative rows of the same gain.
"""
import math
import os
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

LIN_MV = 5.0            # linear x range either side of the reference (mV)
PER_DECADE = 30         # log bins per decade
MAX_PLOTTED = 12        # rows drawn at once
MAX_LIN_BINS = 20_000   # cap on linear bins (finer DAQ steps are merged)
DASH = "–"
LIN_SAMPLES = 20        # plot C: one bin per whole sample up to this many samples, log bins beyond
D_MAX_POINTS = 5000     # plot D: most points drawn per row


def level_counts(trace, i0, i1):
    """(distinct values in mV ascending, their counts) of samples [i0, i1)."""
    codes = np.asarray(trace.codes[i0:i1])
    if len(codes) == 0:
        return np.empty(0), np.empty(0, dtype=np.int64)
    u, c = np.unique(codes, return_counts=True)
    lv = u.astype(np.float64) if trace.scale is None else u / trace.scale
    return lv, c.astype(np.int64)


def _weighted_median(values, weights):
    order = np.argsort(values)
    v, w = np.asarray(values)[order], np.asarray(weights, dtype=np.float64)[order]
    c = np.cumsum(w)
    return float(v[np.searchsorted(c, c[-1] / 2.0)])


def robust_baseline(levels, counts):
    """(median, 1.4826 x median absolute deviation) of the samples given as levels + counts.
    Unlike block statistics this stays at the dark level even when many samples are in pulses
    (a positive luciferase run has ~17 % of its samples above threshold)."""
    lv = np.asarray(levels, dtype=np.float64)
    if len(lv) == 0:
        return math.nan, 0.0
    med = _weighted_median(lv, counts)
    return med, 1.4826 * _weighted_median(np.abs(lv - med), counts)


def daq_step(levels):
    """DAQ output step (mV): the most frequent gap between neighbouring levels, refined over the
    span so 4-decimal rounding of the levels does not bias it; 1.0 with fewer than 2 levels."""
    lv = np.unique(np.asarray(levels, dtype=np.float64))
    if len(lv) < 2:
        return 1.0
    d = np.diff(lv)
    vals, cnt = np.unique(np.round(d, 6), return_counts=True)
    mode = float(vals[np.argmax(cnt)])
    if mode <= 0:
        return float(np.median(d))
    k = round((lv[-1] - lv[0]) / mode)
    return float((lv[-1] - lv[0]) / k) if k > 0 else mode


def _log_edges(lo, hi):
    """lo < hi, both > 0: PER_DECADE edges per decade, lo and hi included."""
    n = max(1, math.ceil(math.log10(hi / lo) * PER_DECADE))
    return np.geomspace(lo, hi, n + 1)


def sample_edges(level_sets, reference, top, bottom):
    """Bin edges for plot A in mV relative to the reference.

    Linear part: bins one DAQ step wide (the smallest step among the level sets, merged so there
    are at most MAX_LIN_BINS) centred on that set's levels, covering +-LIN_MV. Log parts:
    PER_DECADE bins per decade out to top / bottom (absolute mV) when they lie beyond."""
    sets = [np.asarray(s, dtype=np.float64) for s in level_sets if len(s)]
    steps = [(daq_step(s), s) for s in sets if len(s) >= 2]
    if steps:
        step, anchor_set = min(steps, key=lambda x: x[0])
    else:
        step, anchor_set = 1.0, (sets[0] if sets else np.array([reference]))
    step = max(step, 2 * LIN_MV / MAX_LIN_BINS)
    anchor = float(anchor_set[np.argmin(np.abs(anchor_set - reference))]) - reference
    k0 = math.ceil((-LIN_MV - anchor) / step)
    k1 = math.floor((LIN_MV - anchor) / step)
    centres = anchor + step * np.arange(k0, k1 + 1)
    edges = np.concatenate([centres - step / 2, [centres[-1] + step / 2]])
    hi_rel, lo_rel = top - reference, bottom - reference
    if hi_rel > edges[-1]:
        edges = np.concatenate([edges, _log_edges(edges[-1], hi_rel)[1:]])
    if lo_rel < edges[0]:
        edges = np.concatenate([-_log_edges(-edges[0], -lo_rel)[1:][::-1], edges])
    return edges


def sample_density(levels, counts, edges, reference, duration):
    """Samples per second per mV in each bin."""
    hist = np.histogram(np.asarray(levels) - reference, edges, weights=counts)[0]
    return hist / max(duration, 1e-12) / np.diff(edges)


def spike_edges(threshold_rel, top_rel):
    """Log bin edges for plot B from threshold_rel up to top_rel (relative mV); None if the
    threshold is not above the reference."""
    if threshold_rel <= 0:
        return None
    if top_rel <= threshold_rel:
        return np.array([threshold_rel, threshold_rel * 10 ** (1 / PER_DECADE)])
    return _log_edges(threshold_rel, top_rel)


def _finite_pos(sets):
    parts = [np.asarray(w, dtype=np.float64).ravel() for w in sets]
    w = np.concatenate(parts) if parts else np.empty(0)
    return w[np.isfinite(w) & (w > 0)]


def width_edges(width_sets_us, dt_us):
    """Bin edges (us) for plot C, or None with no finite widths. With a sample interval dt_us (the
    smallest among the plotted rows): one bin per whole sample, centred on k*dt for k = 1 ..
    LIN_SAMPLES, PER_DECADE log bins beyond up to the widest, and log bins below for widths under
    half a sample. Without one (non-uniform sampling): log bins from the narrowest to the widest."""
    w = _finite_pos(width_sets_us)
    if len(w) == 0:
        return None
    lo, hi = float(w.min()), float(w.max())
    if not dt_us or not dt_us > 0:
        if hi <= lo:
            return np.array([lo, lo * 10 ** (1 / PER_DECADE)])
        return _log_edges(lo, hi)
    edges = (np.arange(1, LIN_SAMPLES + 2) - 0.5) * dt_us
    if hi > edges[-1]:
        edges = np.concatenate([edges, _log_edges(edges[-1], hi)[1:]])
    if lo < edges[0]:
        edges = np.concatenate([_log_edges(lo, edges[0])[:-1], edges])
    return edges


def width_density(widths_us, edges, duration):
    """Spikes per second per us in each bin (NaN widths ignored)."""
    w = np.asarray(widths_us, dtype=np.float64)
    hist = np.histogram(w[np.isfinite(w)], edges)[0]
    return hist / max(duration, 1e-12) / np.diff(edges)


def width_stats(widths_us):
    """(median, 25th percentile, 75th percentile, count) of the finite widths; (None, None, None, 0)
    with none."""
    w = np.asarray(widths_us, dtype=np.float64)
    w = w[np.isfinite(w)]
    if len(w) == 0:
        return None, None, None, 0
    q25, med, q75 = np.percentile(w, [25, 50, 75])
    return float(med), float(q25), float(q75), len(w)


def subsample(n, cap=None, seed=0):
    """Sorted indices of at most cap (default D_MAX_POINTS) of n points, the same every call."""
    cap = D_MAX_POINTS if cap is None else cap
    if n <= cap:
        return np.arange(n)
    return np.sort(np.random.default_rng(seed).choice(n, cap, replace=False))


def reference_level(baselines, roles):
    """Median baseline of the negative rows, else of all rows."""
    neg = [b for b, r in zip(baselines, roles) if r == "negative" and math.isfinite(b)]
    vals = neg or [b for b in baselines if math.isfinite(b)]
    return float(np.median(vals)) if vals else 0.0


@dataclass
class RowStat:
    row_id: int
    run: object
    gain: object            # float mV or None
    role: str               # "negative", "positive" or "none"
    t_from: float
    t_to: float
    n_spikes: int
    duration: float
    trace_key: object = None     # identifies the file: before/after lines compare rows of one trace

    @property
    def rate(self):
        return self.n_spikes / self.duration if self.duration > 0 else 0.0

    @property
    def err(self):
        return math.sqrt(self.n_spikes) / self.duration if self.duration > 0 else 0.0


@dataclass
class Comparison:
    x_negative: object = None    # rate / negative mean
    sd_above: object = None      # (rate - mean) / SD
    above_limit: object = None   # rate > mean + 3 SD


def _gain_key(g):
    return None if g is None or not math.isfinite(g) else round(float(g))


def compare(rows):
    """({row_id: Comparison}, summary lines). Only positive rows get comparisons, against the
    negative rows of the same gain (rounded to 1 mV); rows without a gain form their own group."""
    comp = {r.row_id: Comparison() for r in rows}
    groups = defaultdict(list)
    for r in rows:
        groups[_gain_key(r.gain)].append(r)
    lines = []
    for key in sorted(groups, key=lambda k: (k is None, -(k or 0))):
        g = groups[key]
        neg = [r.rate for r in g if r.role == "negative"]
        pos = [r for r in g if r.role == "positive"]
        mean = float(np.mean(neg)) if neg else None
        sd = float(np.std(neg, ddof=1)) if len(neg) >= 2 else None
        limit = mean + 3 * sd if sd is not None else None
        for r in pos:
            c = comp[r.row_id]
            if mean:
                c.x_negative = r.rate / mean
            if sd:
                c.sd_above = (r.rate - mean) / sd
            if limit is not None:
                c.above_limit = r.rate > limit
        head = f"{key:g} mV:" if key is not None else "Gain unknown:"
        if neg:
            stats = (f"(mean {mean:.1f}/s, SD {sd:.1f}, limit {limit:.1f}/s)" if sd is not None
                     else f"(mean {mean:.1f}/s, SD {DASH}, limit {DASH})")
            line = f"{head} {len(neg)} negative {stats} · {len(pos)} positive"
        else:
            line = f"{head} 0 negative · {len(pos)} positive"
        if neg and pos and max(neg) > 0:
            line += f" · weakest positive ÷ strongest negative = {min(r.rate for r in pos) / max(neg):.0f}×"
        lines.append(line)
    by_trace = defaultdict(list)
    for r in rows:
        by_trace[r.trace_key if r.trace_key is not None else ("row", r.row_id)].append(r)

    def order(key):
        first = by_trace[key][0]
        return (first.run is None, first.run if first.run is not None else 0, str(key))
    for key in sorted(by_trace, key=order):
        rs = sorted(by_trace[key], key=lambda r: r.t_from)
        first = rs[0]
        name = f"run {first.run}" if first.run is not None else os.path.basename(str(key))
        for r in rs[1:]:
            if first.rate > 0:
                lines.append(f"{name}: {r.t_from:g}–{r.t_to:g} s ÷ {first.t_from:g}–{first.t_to:g} s"
                             f" = {r.rate / first.rate:.1f}× ({r.rate:,.0f} ± {r.err:,.0f} vs "
                             f"{first.rate:,.0f} ± {first.err:,.0f} /s)")
    return comp, lines
