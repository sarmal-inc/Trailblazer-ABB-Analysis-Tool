"""Noise analysis maths (numpy only, no Tk): Welch PSD, tone amplitudes, peaks."""
import functools
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np

SEG = 1 << 19          # Welch segment length (samples)
WORKERS = max(1, min(4, os.cpu_count() or 1))   # threads for the Welch segments
HALF_BINS = 3          # bins either side of a tone summed for its amplitude
PEAK_SEP_HZ = 5.0      # peaks closer than this to a chosen peak are skipped
N_PEAKS = 5            # strongest peaks reported per trace
SEEN_FACTOR = 4.0      # a peak is "seen" in a slice at >= this x the local background power
BG_HALF_BINS = 50      # widest background flank either side of a peak's core (bins)
MIN_FLANK_BINS = 8     # narrower flanks (peak too close to DC or Nyquist): "seen" not judged
SEEN_RULE = ("\u2265 4\u00d7 local background (geometric mean of the medians of equal flanks either "
             "side, up to 50 bins); not judged with 1 slice or within ~12 bins of DC/Nyquist")


@functools.lru_cache(maxsize=4)
def _hann(seg):
    """(periodic Hann window of length seg, read-only; sum(w**2))."""
    w = np.hanning(seg + 1)[:-1]
    w.flags.writeable = False
    return w, float(np.sum(w ** 2))


def _welch(fill, n, fs, seg, keep_slices=False):
    """Welch PSD of n samples; fill(a, b, out) writes samples [a, b) into the float64 out.

    With keep_slices, also returns the single-segment PSDs (float32, same scaling) of the
    non-overlapping slices, i.e. the even-numbered segments: (freqs, psd, df, slices).

    Segments are transformed on up to WORKERS threads (numpy's FFT and ufuncs release the GIL)
    and summed in segment order, so the result is identical to a single-threaded loop.
    """
    if n < 2:
        raise ValueError("at least 2 samples are required")
    seg = min(seg, n)
    hop = max(seg // 2, 1)
    nseg = (n - seg) // hop + 1
    w, wpow = _hann(seg)

    def power(i):
        s = fill(i * hop, i * hop + seg, np.empty(seg))
        s -= s.mean()
        s *= w
        x = np.fft.rfft(s)
        return x.real ** 2 + x.imag ** 2

    acc = np.zeros(seg // 2 + 1)
    scale = 2.0 / (fs * wpow)
    slices = np.empty(((nseg + 1) // 2, seg // 2 + 1), np.float32) if keep_slices else None

    def add(i, p):
        nonlocal acc
        acc += p
        if slices is not None and i % 2 == 0:
            slices[i // 2] = p * scale

    workers = min(WORKERS, nseg)
    if workers > 1:
        step = 4 * workers       # bounds the per-segment results held at once
        with ThreadPoolExecutor(workers) as ex:
            for a in range(0, nseg, step):
                idx = range(a, min(a + step, nseg))
                for i, p in zip(idx, ex.map(power, idx)):
                    add(i, p)
    else:
        for i in range(nseg):
            add(i, power(i))
    psd = acc * (2.0 / (fs * wpow * nseg))
    df = fs / seg
    freqs = np.arange(seg // 2 + 1) * df
    if keep_slices:
        return freqs, psd, df, slices
    return freqs, psd, df


def spectrum(values, fs, seg=SEG, slices=False):
    """Welch PSD. Returns (freqs, psd in mV^2/Hz, df).

    Hann window, 50 % overlap, per-segment mean removed. The one-sided PSD
    is scaled by 2 / (fs * sum(w**2)); the DC and Nyquist bins are doubled
    too (negligible: peaks are searched well above DC and below Nyquist).
    """
    def fill(a, b, out):
        np.copyto(out, values[a:b])
        return out
    return _welch(fill, len(values), fs, seg, slices)


def trace_spectrum(trace, i0, i1, seg=SEG, slices=False):
    """spectrum(trace.values(i0, i1), 1 / trace.dt), decoding one segment at a time."""
    return _welch(lambda a, b, out: trace.values_into(i0 + a, i0 + b, out), i1 - i0,
                  1.0 / trace.dt, seg, slices)


def component_rms(freqs, psd, df, f):
    """(peak bin frequency, mV RMS) of the tone near f, or None if out of range."""
    if f > freqs[-1] or f < df:
        return None
    c = int(round(f / df))
    lo = max(c - HALF_BINS, 0)
    hi = min(c + HALF_BINS + 1, len(psd))
    j = lo + int(np.argmax(psd[lo:hi]))
    a = max(j - HALF_BINS, 0)
    b = min(j + HALF_BINS + 1, len(psd))
    return float(freqs[j]), float(np.sqrt(np.sum(psd[a:b]) * df))


def top_peaks(freqs, psd, df, n=3, f_min=2.0, sep=PEAK_SEP_HZ):
    """Up to n (Hz, mV RMS) peaks, strongest first, at least sep Hz apart."""
    start = int(np.searchsorted(freqs, f_min))
    if start >= len(psd):
        return []
    order = np.argsort(psd[start:])[::-1] + start
    chosen = []
    for j in order:
        f = float(freqs[j])
        if any(abs(f - c[0]) < sep for c in chosen):
            continue
        res = component_rms(freqs, psd, df, f)
        if res is None or res[0] < f_min:
            continue
        if any(abs(res[0] - c[0]) < sep for c in chosen):
            continue
        chosen.append(res)
        if len(chosen) == n:
            break
    return chosen


def seen_count(sl, df, f):
    """Number of slices (rows of sl) in which the tone at f stands out from its background,
    or None when that cannot be judged.

    Core = the slice's power summed over the HALF_BINS bins either side of j = round(f/df).
    Background per bin = geometric mean of the medians of two equal flanks (w bins each, just
    outside the core, w <= BG_HALF_BINS) / ln 2 (median -> mean of an exponential noise bin).
    The geometric mean follows a sloped (e.g. 1/f) background, so a slope is not mistaken
    for a tone. Seen when core >= SEEN_FACTOR x the background of the same number of bins.

    None when there is only one slice (the peak was picked from that very spectrum, so it
    would look "seen" by selection alone) or when a flank would be narrower than
    MIN_FLANK_BINS (too close to DC or Nyquist). DC is never part of a flank.
    """
    n_slices, nbins = sl.shape
    if n_slices < 2:
        return None
    last = nbins - 1
    j = min(max(int(round(f / df)), 0), last)
    w = min(BG_HALF_BINS, j - HALF_BINS - 1, last - (j + HALF_BINS))
    if w < MIN_FLANK_BINS:
        return None
    left = sl[:, j - HALF_BINS - w:j - HALF_BINS].astype(np.float64)
    right = sl[:, j + HALF_BINS + 1:j + HALF_BINS + 1 + w].astype(np.float64)
    bg = np.sqrt(np.median(left, axis=1) * np.median(right, axis=1)) / math.log(2)
    core_power = sl[:, j - HALF_BINS:j + HALF_BINS + 1].sum(axis=1, dtype=np.float64)
    return int(np.count_nonzero(core_power >= SEEN_FACTOR * (2 * HALF_BINS + 1) * bg))


def amplitude_spectrum(psd, df):
    """Amplitude spectral view: sqrt(psd * df)."""
    return np.sqrt(psd * df)


DEAD_TIME = 50e-6        # s; crossings this soon after a spike's last sample join that spike
SPIKE_LIST_CAP = 10_000  # display cap only; SpikeResult holds every spike
NOTE_NOT_UNIFORM = "Spectrum needs evenly spaced samples."
NOTE_EMPTY = "No samples in the window"


@dataclass
class SpikeResult:
    baseline: float
    sigma: float
    threshold: float
    times: np.ndarray    # time of each spike's highest sample (every spike, uncapped)
    heights: np.ndarray  # that sample minus baseline (mV)
    duration: float      # window duration (s) the rate is relative to


@dataclass
class NoiseResult:
    baseline: float
    sigma: float
    threshold: float
    n_spikes: int
    rate: float
    median_spacing: float | None
    spacing_hz: float | None
    peaks: list
    watch: dict
    resolution_hz: float | None
    spikes: SpikeResult | None
    note: str
    k: float = math.nan   # spike threshold factor this result was computed with
    seen: list = field(default_factory=list)   # per peak: slices it was seen in (None = not judged)
    n_slices: int | None = None                # non-overlapping ~1 s slices in the window


def spike_baseline(trace, i0, i1):
    """(baseline, sigma) of samples [i0, i1) from the cache's block table.

    Median of the whole-block means and of the whole-block sqrt(m2/n); with no whole block, the
    median and the median 64-sample-chunk std of the raw samples. The median-of-blocks estimate
    assumes fewer than half the blocks contain spikes (true for sparse events).
    """
    rows, head, tail = trace.block_rows(i0, i1)
    if len(rows):
        return (float(np.median(rows["mean"])),
                float(np.median(np.sqrt(rows["m2"] / rows["n"]))))
    v = np.asarray(trace.values(*head), dtype=np.float64)
    if len(v) == 0:
        return math.nan, 0.0
    m = len(v) // 64
    if m == 0:
        return float(np.median(v)), float(v.std())
    chunks = v[:m * 64].reshape(m, 64)
    return float(np.median(v)), float(np.median(chunks.std(axis=1)))


def _empty_spikes(baseline, sigma, threshold, duration):
    return SpikeResult(baseline, sigma, threshold, np.empty(0), np.empty(0), duration)


def join_gap(dt, dead_time=DEAD_TIME):
    """Largest gap (s) between above-threshold samples that still belong to one spike:
    max(dead_time, 1.5 sample intervals), so neighbouring samples always join; dead_time alone
    for traces without a uniform sample interval."""
    return dead_time if dt is None else max(dead_time, 1.5 * dt)


def find_spikes(trace, t0, t1, k, dead_time=DEAD_TIME):
    """Upward spikes above baseline + k*sigma in the window [t0, t1] (see find_spikes_at)."""
    i0, i1 = trace.index_range(t0, t1)
    if i1 <= i0:
        return find_spikes_at(trace, t0, t1, math.nan, dead_time)
    baseline, sigma = spike_baseline(trace, i0, i1)
    return find_spikes_at(trace, t0, t1, baseline + k * sigma, dead_time, _base=(baseline, sigma))


def find_spikes_at(trace, t0, t1, threshold, dead_time=DEAD_TIME, _base=None):
    """Upward spikes above a given threshold (mV) in the window [t0, t1].

    Raw samples are read only for the partial head/tail blocks and for whole blocks whose vmax
    exceeds the threshold (adjacent ones are read as one run). Samples above the threshold are
    grouped into spikes: a gap of at most join_gap(dt, dead_time) between consecutive such samples
    keeps them in one spike. Each spike is reported at its highest sample (first on ties); heights
    are relative to the window's baseline (spike_baseline).
    """
    i0, i1 = trace.index_range(t0, t1)
    duration = max(min(t1, trace.t_end) - max(t0, trace.t_start), 0.0) if trace.n else 0.0
    if i1 <= i0:
        return _empty_spikes(math.nan, 0.0, math.nan, duration)
    baseline, sigma = _base if _base is not None else spike_baseline(trace, i0, i1)
    rows, head, tail = trace.block_rows(i0, i1)
    B = trace.block
    spans = [head, tail]
    if len(rows):
        # small margin: block extremes are computed from the unquantised values
        cand = np.flatnonzero(rows["vmax"] > threshold - 1e-6)
        first = head[1] // B
        for b in cand + first:
            spans.append((int(b) * B, int(b) * B + B))
    spans = sorted(s for s in spans if s[1] > s[0])
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    idx_parts, val_parts = [], []
    for a, b in merged:
        v = np.asarray(trace.values(a, b), dtype=np.float64)
        hit = np.flatnonzero(v > threshold)
        idx_parts.append(hit + a)
        val_parts.append(v[hit])
    if not idx_parts:
        return _empty_spikes(baseline, sigma, threshold, duration)
    idx = np.concatenate(idx_parts)
    if len(idx) == 0:
        return _empty_spikes(baseline, sigma, threshold, duration)
    val = np.concatenate(val_parts)
    t = np.asarray(trace.times_at(idx), dtype=np.float64)
    starts = np.concatenate(([0], np.flatnonzero(np.diff(t) > join_gap(trace.dt, dead_time) + 1e-12) + 1))
    sizes = np.diff(np.append(starts, len(idx)))
    peak_val = np.maximum.reduceat(val, starts)
    gid = np.repeat(np.arange(len(starts)), sizes)
    at_max = np.flatnonzero(val == peak_val[gid])
    first_max = at_max[np.unique(gid[at_max], return_index=True)[1]]
    return SpikeResult(baseline, sigma, threshold, t[first_max], val[first_max] - baseline,
                       duration)


def analyze(trace, t0, t1, watch_hz, k, f_min):
    """Spike statistics and spectrum peaks/watch amplitudes of one trace window."""
    i0, i1 = trace.index_range(t0, t1)
    if i1 - i0 < 2:
        return NoiseResult(math.nan, math.nan, math.nan, 0, 0.0, None, None, [],
                           {float(f): None for f in watch_hz}, None, None, NOTE_EMPTY, k)
    sp = find_spikes(trace, t0, t1, k)
    n = len(sp.times)
    spacing = float(np.median(np.diff(sp.times))) if n >= 2 else None
    spacing_hz = 1.0 / spacing if spacing else None
    rate = n / sp.duration if sp.duration > 0 else 0.0
    peaks, watch, res, note = [], {float(f): None for f in watch_hz}, None, ""
    seen, n_slices = [], None
    if trace.dt is None:
        note = NOTE_NOT_UNIFORM
    elif i1 - i0 >= 2:
        freqs, psd, df, sl = trace_spectrum(trace, i0, i1, slices=True)
        peaks = top_peaks(freqs, psd, df, n=N_PEAKS, f_min=f_min)
        seen = [seen_count(sl, df, hz) for hz, _ in peaks]
        n_slices = int(sl.shape[0])
        del sl
        for f in watch:
            r = component_rms(freqs, psd, df, f)
            watch[f] = None if r is None else r[1]
        res = df
    return NoiseResult(sp.baseline, sp.sigma, sp.threshold, n, rate, spacing, spacing_hz,
                       peaks, watch, res, sp, note, k, seen, n_slices)
