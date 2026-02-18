import numpy as np
import pandas as pd

def build_vp_ohlc(df, n_bins=1000, body_w=0.70):
    pmin, pmax = df["low"].min(), df["high"].max()
    if not np.isfinite(pmin) or not np.isfinite(pmax) or pmax <= pmin:
        return None, None

    edges = np.linspace(pmin, pmax, n_bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    vp = np.zeros(n_bins, dtype=float)

    def add_segment(lo, hi, v):
        if v <= 0 or hi <= lo:
            return
        i0 = max(0, np.searchsorted(edges, lo) - 1)
        i1 = min(n_bins, np.searchsorted(edges, hi))
        span = i1 - i0
        if span > 0:
            vp[i0:i1] += v / span

    wick_w = 1.0 - body_w

    for _, r in df.iterrows():
        vol = float(r["volume"])
        if vol <= 0:
            continue

        o, c, h, l = float(r["open"]), float(r["close"]), float(r["high"]), float(r["low"])
        body_lo, body_hi = (o, c) if o < c else (c, o)

        # 1) cuerpo
        add_segment(body_lo, body_hi, vol * body_w)

        # 2) mechas proporcional a longitudes
        up = max(0.0, h - body_hi)
        dn = max(0.0, body_lo - l)
        s = up + dn
        if s > 0:
            add_segment(body_hi, h, vol * wick_w * (up / s))
            add_segment(l, body_lo, vol * wick_w * (dn / s))

    return centers, vp


def value_area(centers, vp, pct=0.70):
    total = vp.sum()
    if total <= 0:
        return None, None, None

    poc_i = int(np.argmax(vp))
    target = total * pct

    lo = hi = poc_i
    acc = vp[poc_i]

    # expandir agregando el lado adyacente con más volumen
    while acc < target and (lo > 0 or hi < len(vp) - 1):
        left = vp[lo - 1] if lo > 0 else -1
        right = vp[hi + 1] if hi < len(vp) - 1 else -1

        if right > left:
            hi += 1
            acc += vp[hi]
        else:
            lo -= 1
            acc += vp[lo]

    return centers[poc_i], centers[lo], centers[hi]


def find_peaks_simple(centers, vp, smooth=5, min_sep_bins=10, thr_q=0.85):
    if vp.sum() <= 0:
        return []

    # suavizado (media móvil)
    k = int(max(1, smooth))
    if k > 1:
        kernel = np.ones(k) / k
        vps = np.convolve(vp, kernel, mode="same")
    else:
        vps = vp

    # umbral (quantile) para quedarte con nodos relevantes
    thr = np.quantile(vps[vps > 0], thr_q) if np.any(vps > 0) else 0

    candidates = []
    for i in range(1, len(vps) - 1):
        if vps[i] > vps[i - 1] and vps[i] > vps[i + 1] and vps[i] >= thr:
            candidates.append(i)

    # aplicar separación mínima: greedy por volumen descendente
    candidates = sorted(candidates, key=lambda i: vps[i], reverse=True)
    chosen = []
    for i in candidates:
        if all(abs(i - j) >= min_sep_bins for j in chosen):
            chosen.append(i)

    chosen = sorted(chosen)
    return [(float(centers[i]), float(vps[i])) for i in chosen]


def vp_features_compose(df:pd.DataFrame , fecha_inicio:str, freq:str="1H", n_bins:int=1000, body_w:float=0.70, va_pct:float=0.70):
    
    df = df.copy()
    df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)

    df = df.sort_values("dt").set_index("dt")

    df = df[df.index >= pd.Timestamp(fecha_inicio, tz="UTC")]

    if df.empty:
        return None
    
    centers, vp = build_vp_ohlc(df, n_bins=n_bins, body_w=body_w)
    if centers is None:
        return None
    
    poc, val, vah = value_area(centers, vp, pct=va_pct)

    peaks = find_peaks_simple(centers, vp, smooth=7, min_sep_bins=15, thr_q=0.85)

    return {
        "poc": poc,
        "val": val,
        "vah": vah,
        "total_volume": float(vp.sum()),
        "peaks": peaks
    }

    