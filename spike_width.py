"""Spike width (numpy only, no Tk): full width at half maximum of each spike.

Half max = baseline + (peak - baseline) / 2. From the peak sample, each side looks for the nearest
sample below half max, no further than the walk limit, the window [i0, i1) and the neighbouring
spike's peak (exclusive). The crossing time is interpolated linearly between that sample and its
inward neighbour. A spike with no crossing on a side within those bounds is "overlapped": NaN.
See docs/superpowers/specs/2026-11-23-spike-width-design.md.
"""
import math

import numpy as np

WALK_LIMIT_S = 200e-6     # furthest the walk goes on each side (s)
MIN_WALK_SAMPLES = 5      # ... but at least this many samples (slow sample rates)
CHUNK = 20_000            # spikes processed at once (bounds memory)


def _walk_samples(trace, i0, i1, walk_limit_s):
    dt = trace.dt
    if dt is None:
        if i1 - i0 < 2:
            return MIN_WALK_SAMPLES
        span = float(trace.times_at(np.array([i1 - 1]))[0] - trace.times_at(np.array([i0]))[0])
        dt = span / (i1 - 1 - i0)
    if not dt > 0:
        return MIN_WALK_SAMPLES
    return max(MIN_WALK_SAMPLES, int(math.ceil(walk_limit_s / dt - 1e-9)))


def _times(trace, idx):
    return np.asarray(trace.times_at(idx), dtype=np.float64)


def fwhm(trace, peak_idx, baseline, i0, i1, walk_limit_s=WALK_LIMIT_S, chunk=CHUNK):
    """FWHM (s) of each spike, NaN where overlapped. peak_idx: ascending sample indices of the
    spikes' highest samples, all inside [i0, i1); baseline in mV."""
    peaks = np.asarray(peak_idx, dtype=np.int64)
    out = np.full(len(peaks), np.nan)
    if len(peaks) == 0:
        return out
    L = _walk_samples(trace, i0, i1, walk_limit_s)
    offs = np.arange(-L, L + 1)
    prev = np.concatenate(([i0 - 1], peaks[:-1]))
    nxt = np.concatenate((peaks[1:], [i1]))
    lo_all = np.maximum(i0, prev + 1)
    hi_all = np.minimum(i1 - 1, nxt - 1)
    for a in range(0, len(peaks), max(1, int(chunk))):
        p = peaks[a:a + chunk]
        lo, hi = lo_all[a:a + chunk, None], hi_all[a:a + chunk, None]
        m = p[:, None] + offs
        valid = (m >= lo) & (m <= hi)
        v = np.asarray(trace.values_at(np.clip(m, 0, trace.n - 1)), dtype=np.float64)
        vpk = v[:, L]
        half = baseline + (vpk - baseline) / 2.0
        below = valid & (v < half[:, None])
        left, right = below[:, :L], below[:, L + 1:]
        ok = left.any(axis=1) & right.any(axis=1)
        if not ok.any():
            continue
        rows = np.flatnonzero(ok)
        jl = L - 1 - np.argmax(left[rows, ::-1], axis=1)      # nearest below-half sample, left
        jr = L + 1 + np.argmax(right[rows], axis=1)           # ... and right
        h = half[rows]
        vl, vl_in = v[rows, jl], v[rows, jl + 1]
        vr, vr_in = v[rows, jr], v[rows, jr - 1]
        gl, gr = p[rows] + jl - L, p[rows] + jr - L
        tl, tl_in = _times(trace, gl), _times(trace, gl + 1)
        tr_, tr_in = _times(trace, gr), _times(trace, gr - 1)
        t_left = tl + (h - vl) / (vl_in - vl) * (tl_in - tl)
        t_right = tr_in + (vr_in - h) / (vr_in - vr) * (tr_ - tr_in)
        out[a + rows] = t_right - t_left
    return out
