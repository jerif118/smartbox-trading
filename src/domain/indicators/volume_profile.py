"""
Volume Profile. Regla #5:
- POC (Point of Control)
- VAH, VAL (Value Area High/Low al 70%)
- Peaks
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

DEFAULT_VA_PCT = 0.70
DEFAULT_N_BINS = 1000
DEFAULT_BODY_W = 0.70


@dataclass(frozen=True)
class VolumeProfile:
    poc: float
    vah: float
    val: float
    total_volume: float
    peaks: list[tuple[float, float]]


def _build_vp_ohlc(
    df: pd.DataFrame,
    n_bins: int = DEFAULT_N_BINS,
    body_w: float = DEFAULT_BODY_W,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    pmin, pmax = float(df["low"].min()), float(df["high"].max())
    if not np.isfinite(pmin) or not np.isfinite(pmax) or pmax <= pmin:
        return None, None

    edges = np.linspace(pmin, pmax, n_bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    vp = np.zeros(n_bins, dtype=float)

    def add_segment(lo: float, hi: float, v: float) -> None:
        if v <= 0 or hi <= lo:
            return
        i0 = max(0, np.searchsorted(edges, lo) - 1)
        i1 = min(n_bins, np.searchsorted(edges, hi))
        span = i1 - i0
        if span > 0:
            vp[i0:i1] += v / span

    wick_w = 1.0 - body_w

    for _, r in df.iterrows():
        vol = float(r.get("volume", 0))
        if vol <= 0:
            continue
        o, c, h, l = (
            float(r["open"]),
            float(r["close"]),
            float(r["high"]),
            float(r["low"]),
        )
        body_lo, body_hi = (o, c) if o < c else (c, o)
        add_segment(body_lo, body_hi, vol * body_w)
        up = max(0.0, h - body_hi)
        dn = max(0.0, body_lo - l)
        s = up + dn
        if s > 0:
            add_segment(body_hi, h, vol * wick_w * (up / s))
            add_segment(l, body_lo, vol * wick_w * (dn / s))

    return centers, vp


def _value_area(
    centers: np.ndarray, vp: np.ndarray, pct: float = DEFAULT_VA_PCT
) -> tuple[float | None, float | None, float | None]:
    total = float(vp.sum())
    if total <= 0:
        return None, None, None
    poc_i = int(np.argmax(vp))
    target = total * pct
    lo = hi = poc_i
    acc = float(vp[poc_i])
    while acc < target and (lo > 0 or hi < len(vp) - 1):
        left = float(vp[lo - 1]) if lo > 0 else -1.0
        right = float(vp[hi + 1]) if hi < len(vp) - 1 else -1.0
        if right > left:
            hi += 1
            acc += float(vp[hi])
        else:
            lo -= 1
            acc += float(vp[lo])
    return float(centers[poc_i]), float(centers[lo]), float(centers[hi])


def _find_peaks(
    centers: np.ndarray,
    vp: np.ndarray,
    smooth: int = 5,
    min_sep_bins: int = 10,
    thr_q: float = 0.85,
) -> list[tuple[float, float]]:
    if vp.sum() <= 0:
        return []
    k = max(1, int(smooth))
    if k > 1:
        kernel = np.ones(k) / k
        vps = np.convolve(vp, kernel, mode="same")
    else:
        vps = vp.copy()
    pos = vps[vps > 0]
    thr = float(np.quantile(pos, thr_q)) if pos.size else 0.0
    candidates: list[int] = []
    for i in range(1, len(vps) - 1):
        if vps[i] > vps[i - 1] and vps[i] > vps[i + 1] and vps[i] >= thr:
            candidates.append(i)
    candidates = sorted(candidates, key=lambda i: vps[i], reverse=True)
    chosen: list[int] = []
    for i in candidates:
        if all(abs(i - j) >= min_sep_bins for j in chosen):
            chosen.append(i)
    chosen.sort()
    return [(float(centers[i]), float(vps[i])) for i in chosen]


def compute_volume_profile(
    df: pd.DataFrame,
    va_pct: float = DEFAULT_VA_PCT,
    n_bins: int = DEFAULT_N_BINS,
    body_w: float = DEFAULT_BODY_W,
) -> VolumeProfile | None:
    """Regla #5: POC, VAH, VAL, peaks."""
    if df is None or df.empty:
        return None
    df = df.copy()
    if "volume" not in df.columns:
        return None

    centers, vp = _build_vp_ohlc(df, n_bins=n_bins, body_w=body_w)
    if centers is None:
        return None

    poc, val, vah = _value_area(centers, vp, pct=va_pct)
    if poc is None:
        return None

    peaks = _find_peaks(centers, vp)

    return VolumeProfile(
        poc=poc,
        vah=vah,
        val=val,
        total_volume=float(vp.sum()),
        peaks=peaks,
    )
