"""
detect_ttr_v2.py

Step-1 detector for TTR / overlapped bars.

Design goal:
- Do NOT detect breakout, trap, wedge, bear flag, or strategy.
- Detect overlap-range candidates only.
- Separate two primitive types:
  1) absolute_overlap_ttr_candidate
     Bars are compact and heavily overlapping inside a relatively narrow range.
  2) stair_overlap_ttr_candidate
     Bars still overlap, but the range drifts/stairs upward or downward.

Important change from v1:
- v1 used greedy non-overlap selection and often swallowed a trend leg plus later TTR
  into one large low-efficiency window.
- v2 uses weighted interval scheduling so multiple better sub-ranges can beat one
  broad parent range.

Supported OHLC formats:
1) Datetime index + Message column containing "open,high,low,close"
2) Datetime column/index + open/high/low/close columns
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

VERSION = "2026-05-17-detect-ttr-v2-absolute-stair"

# -----------------------------
# Loading
# -----------------------------

def normalize_datetime_index(
    df: pd.DataFrame,
    datetime_col: Optional[str] = None,
    source_tz_if_naive: str = "UTC",
    target_tz: str = "Asia/Bangkok",
    round_to_minute: bool = False,
) -> pd.DataFrame:
    df = df.copy()
    if datetime_col:
        if datetime_col not in df.columns:
            raise ValueError(f"datetime_col '{datetime_col}' not found. Columns={list(df.columns)}")
        dt = pd.to_datetime(df[datetime_col], errors="coerce")
        df = df.drop(columns=[datetime_col])
    else:
        dt = pd.to_datetime(df.index, errors="coerce")
    if dt.isna().any():
        raise ValueError(f"Datetime parsing failed for {int(dt.isna().sum())} row(s).")
    idx = pd.DatetimeIndex(dt)
    if idx.tz is None:
        idx = idx.tz_localize(source_tz_if_naive).tz_convert(target_tz)
    else:
        idx = idx.tz_convert(target_tz)
    idx = idx.tz_localize(None)
    if round_to_minute:
        idx = idx.round("min")
    df.index = idx
    return df.sort_index()


def normalize_ohlc_columns(df: pd.DataFrame, message_col: str = "Message") -> pd.DataFrame:
    df = df.copy()
    if message_col in df.columns:
        parts = df[message_col].astype(str).str.split(",", expand=True)
        if parts.shape[1] < 4:
            raise ValueError(f"Column '{message_col}' must contain open,high,low,close")
        out = parts.iloc[:, :4].astype(float)
        out.columns = ["open", "high", "low", "close"]
        out.index = df.index
        return out.dropna()
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    required = ["open", "high", "low", "close"]
    missing = [c for c in required if c not in lower_map]
    if missing:
        raise ValueError(
            "Expected either Message='open,high,low,close' or columns open/high/low/close. "
            f"Missing={missing}, Columns={list(df.columns)}"
        )
    out = df[[lower_map[c] for c in required]].copy()
    out.columns = required
    return out.astype(float).dropna()


def load_ohlc_file(
    input_path: str | Path,
    datetime_col: Optional[str] = None,
    message_col: str = "Message",
    source_tz_if_naive: str = "UTC",
    target_tz: str = "Asia/Bangkok",
    round_to_minute: bool = False,
) -> pd.DataFrame:
    input_path = Path(input_path)
    if input_path.suffix.lower() == ".csv":
        raw = pd.read_csv(input_path, index_col=0 if datetime_col is None else None)
    elif input_path.suffix.lower() in (".xlsx", ".xls"):
        raw = pd.read_excel(input_path, index_col=0 if datetime_col is None else None)
    else:
        raise ValueError("Supported input files: .csv, .xlsx, .xls")
    raw = normalize_datetime_index(
        raw,
        datetime_col=datetime_col,
        source_tz_if_naive=source_tz_if_naive,
        target_tz=target_tz,
        round_to_minute=round_to_minute,
    )
    return normalize_ohlc_columns(raw, message_col=message_col)


def resample_ohlc(df: pd.DataFrame, rule: Optional[str]) -> pd.DataFrame:
    if not rule:
        return df
    return df.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


def slice_ohlc(df: pd.DataFrame, start: Optional[str], end: Optional[str]) -> pd.DataFrame:
    if start or end:
        df = df.loc[start:end].copy()
    if df.empty:
        raise ValueError(f"No OHLC rows found between start={start} and end={end}")
    return df

# -----------------------------
# Metrics + scanner
# -----------------------------

def safe_float(x: float) -> float:
    if x is None or np.isnan(x) or np.isinf(x):
        return 0.0
    return float(x)


@dataclass
class TTRCandidate:
    start: str
    end: str
    start_i: int
    end_i: int
    bars: int
    kind: str
    score: float
    range_high: float
    range_low: float
    range_height: float
    typical_bar_range: float
    range_units: float
    adjacent_overlap: float
    body_overlap: float
    common_overlap: float
    directional_efficiency: float
    alternating_bar_ratio: float
    slope_units_per_bar: float
    new_extreme_rate: float


def metrics_from_arrays(o, h, l, c) -> dict:
    n = len(c)
    ranges = h - l
    typical = float(np.median(ranges)) if n else 0.0
    if typical <= 0:
        typical = float(np.mean(ranges)) if n else 0.0
    range_high = float(np.max(h))
    range_low = float(np.min(l))
    height = range_high - range_low
    range_units = height / typical if typical > 0 else 0.0

    if n >= 2:
        overlap = np.maximum(0.0, np.minimum(h[:-1], h[1:]) - np.maximum(l[:-1], l[1:]))
        denom = np.maximum(h[1:] - l[1:], 1e-12)
        adj = float(np.mean(overlap / denom))

        p_low = np.minimum(o[:-1], c[:-1]); p_high = np.maximum(o[:-1], c[:-1])
        b_low = np.minimum(o[1:], c[1:]); b_high = np.maximum(o[1:], c[1:])
        bo = np.maximum(0.0, np.minimum(p_high, b_high) - np.maximum(p_low, b_low))
        bd = np.maximum(b_high - b_low, 1e-12)
        body_adj = float(np.mean(np.minimum(1.0, bo / bd)))

        path = float(np.sum(np.abs(np.diff(c))))
        eff = abs(float(c[-1] - c[0])) / path if path > 0 else 0.0

        signs = np.sign(c - o)
        valid = (signs[:-1] != 0) & (signs[1:] != 0)
        alt = float(np.mean(signs[:-1][valid] != signs[1:][valid])) if np.any(valid) else 0.0

        # new extreme rate
        hi = h[0]; lo = l[0]; cnt = 0
        for i in range(1, n):
            if h[i] > hi:
                cnt += 1; hi = h[i]
            if l[i] < lo:
                cnt += 1; lo = l[i]
        ner = cnt / (2 * (n - 1))

        x = np.arange(n, dtype=float)
        slope = float(np.polyfit(x, c.astype(float), 1)[0] / typical) if typical > 0 else 0.0
    else:
        adj = body_adj = eff = alt = ner = slope = 0.0

    common = max(0.0, float(np.min(h) - np.max(l))) / typical if typical > 0 else 0.0

    absolute_limit = max(2.2, np.sqrt(n) * 0.82)
    absolute_score = (
        0.42 * adj
        + 0.28 * (1.0 - min(eff, 1.0))
        + 0.18 * (1.0 / (1.0 + max(0.0, range_units - absolute_limit) / max(absolute_limit, 1e-9)))
        + 0.05 * min(common, 1.0)
        + 0.04 * alt
        + 0.03 * (1.0 - min(ner, 1.0))
    )

    stair_limit = max(4.0, np.sqrt(n) * 1.45)
    slope_score = 1.0 / (1.0 + abs(slope) * 3.0)
    stair_score = (
        0.34 * adj
        + 0.18 * body_adj
        + 0.20 * (1.0 - min(eff, 1.0))
        + 0.16 * (1.0 / (1.0 + max(0.0, range_units - stair_limit) / max(stair_limit, 1e-9)))
        + 0.08 * slope_score
        + 0.04 * (1.0 - min(ner, 1.0))
    )

    return {
        "bars": int(n), "range_high": safe_float(range_high), "range_low": safe_float(range_low),
        "range_height": safe_float(height), "typical_bar_range": safe_float(typical),
        "range_units": safe_float(range_units), "adjacent_overlap": safe_float(adj),
        "body_overlap": safe_float(body_adj), "common_overlap": safe_float(common),
        "directional_efficiency": safe_float(eff), "alternating_bar_ratio": safe_float(alt),
        "slope_units_per_bar": safe_float(slope), "new_extreme_rate": safe_float(ner),
        "absolute_limit": safe_float(absolute_limit), "stair_limit": safe_float(stair_limit),
        "absolute_score": safe_float(absolute_score), "stair_score": safe_float(stair_score),
    }


def candidate_kind(m: dict, mode: str) -> tuple[Optional[str], float]:
    abs_ok = (
        m["absolute_score"] >= 0.66
        and m["adjacent_overlap"] >= 0.54
        and m["directional_efficiency"] <= 0.34
        and m["range_units"] <= m["absolute_limit"] * 1.18
    )
    stair_ok = (
        m["stair_score"] >= 0.63
        and m["adjacent_overlap"] >= 0.48
        and m["directional_efficiency"] <= 0.52
        and abs(m["slope_units_per_bar"]) <= 0.85
        and m["range_units"] <= m["stair_limit"] * 1.20
    )
    if mode == "absolute":
        return ("absolute_overlap_ttr_candidate", m["absolute_score"]) if abs_ok else (None, 0.0)
    if mode == "stair":
        return ("stair_overlap_ttr_candidate", m["stair_score"]) if stair_ok else (None, 0.0)
    if abs_ok:
        return "absolute_overlap_ttr_candidate", m["absolute_score"]
    if stair_ok:
        return "stair_overlap_ttr_candidate", m["stair_score"]
    return None, 0.0



def pivot_indices_from_arrays(h: np.ndarray, l: np.ndarray, left: int = 3, right: int = 3) -> set[int]:
    """Return local high/low pivot indices. Used only as optional anchors;
    this is not a pattern detector.
    """
    pivots: set[int] = set()
    n = len(h)
    for i in range(n):
        a = max(0, i - left)
        b = min(n, i + right + 1)
        if h[i] >= np.max(h[a:b]) - 1e-12 or l[i] <= np.min(l[a:b]) + 1e-12:
            pivots.add(i)
            if i + 1 < n:
                pivots.add(i + 1)  # allow TTR to start just after climax/pivot bar
    pivots.add(0)
    pivots.add(n - 1)
    return pivots

def scan_ttr_candidates(
    df: pd.DataFrame,
    min_bars: int = 3,
    max_bars: int = 60,
    step: int = 1,
    mode: str = "both",
    anchor_pivots: bool = True,
    pivot_left: int = 3,
    pivot_right: int = 3,
) -> list[TTRCandidate]:
    min_bars = max(3, int(min_bars))
    max_bars = min(int(max_bars), len(df))
    if max_bars < min_bars:
        raise ValueError("max_bars must be >= min_bars")
    o = df.open.to_numpy(float); h = df.high.to_numpy(float); l = df.low.to_numpy(float); c = df.close.to_numpy(float)
    idx = df.index
    out: list[TTRCandidate] = []
    n_total = len(df)
    pivots = pivot_indices_from_arrays(h, l, pivot_left, pivot_right) if anchor_pivots else set(range(n_total))
    for length in range(min_bars, max_bars + 1):
        for start_i in range(0, n_total - length + 1, step):
            end_i = start_i + length - 1
            if anchor_pivots and (start_i not in pivots or end_i not in pivots):
                continue
            m = metrics_from_arrays(o[start_i:end_i+1], h[start_i:end_i+1], l[start_i:end_i+1], c[start_i:end_i+1])
            kind, score = candidate_kind(m, mode)
            if not kind:
                continue
            out.append(TTRCandidate(
                start=str(idx[start_i]), end=str(idx[end_i]), start_i=start_i, end_i=end_i,
                bars=length, kind=kind, score=float(score), range_high=float(m["range_high"]),
                range_low=float(m["range_low"]), range_height=float(m["range_height"]),
                typical_bar_range=float(m["typical_bar_range"]), range_units=float(m["range_units"]),
                adjacent_overlap=float(m["adjacent_overlap"]), body_overlap=float(m["body_overlap"]),
                common_overlap=float(m["common_overlap"]), directional_efficiency=float(m["directional_efficiency"]),
                alternating_bar_ratio=float(m["alternating_bar_ratio"]), slope_units_per_bar=float(m["slope_units_per_bar"]),
                new_extreme_rate=float(m["new_extreme_rate"]),
            ))
    return out


def candidate_weight(c: TTRCandidate, prefer: str = "balanced") -> float:
    if prefer == "short":
        length_part = np.sqrt(c.bars)
    elif prefer == "long":
        length_part = np.log1p(c.bars) * 1.35
    else:
        length_part = np.log1p(c.bars)
    kind_bonus = 1.08 if c.kind.startswith("absolute") else 1.0
    compact_bonus = 1.0 / (1.0 + max(0.0, c.range_units - np.sqrt(c.bars) * 0.82) * 0.10)
    return float(c.score * length_part * kind_bonus * compact_bonus)


def select_weighted_nonoverlap(candidates: list[TTRCandidate], min_gap_bars: int = 0, prefer: str = "balanced", max_candidates: int = 2500) -> list[TTRCandidate]:
    if not candidates:
        return []
    best = {}
    for c in candidates:
        key = (c.start_i, c.end_i, c.kind)
        if key not in best or c.score > best[key].score:
            best[key] = c
    cands = list(best.values())
    if len(cands) > max_candidates:
        cands = sorted(cands, key=lambda c: candidate_weight(c, prefer), reverse=True)[:max_candidates]
    cands = sorted(cands, key=lambda c: (c.end_i, c.start_i))
    ends = [c.end_i for c in cands]
    p = []
    for c in cands:
        limit = c.start_i - min_gap_bars - 1
        p.append(int(np.searchsorted(ends, limit, side="right") - 1))
    n = len(cands)
    dp = [0.0] * (n + 1)
    choose = [False] * n
    weights = [candidate_weight(c, prefer) for c in cands]
    for i in range(1, n + 1):
        take_val = weights[i-1] + dp[p[i-1] + 1]
        skip_val = dp[i-1]
        if take_val > skip_val:
            dp[i] = take_val; choose[i-1] = True
        else:
            dp[i] = skip_val
    selected = []
    i = n - 1
    while i >= 0:
        take_val = weights[i] + dp[p[i] + 1]
        if choose[i] and take_val >= dp[i] - 1e-12:
            selected.append(cands[i]); i = p[i]
        else:
            i -= 1
    selected.reverse()
    return selected


def candidates_to_df(candidates: list[TTRCandidate]) -> pd.DataFrame:
    return pd.DataFrame([asdict(c) for c in candidates]) if candidates else pd.DataFrame()


def print_candidates(candidates: list[TTRCandidate], max_print: int = 60) -> None:
    if not candidates:
        print("No TTR / overlap range candidates found with current thresholds.")
        return
    print(f"Detected TTR / overlap range candidates: {len(candidates)}")
    for i, c in enumerate(candidates[:max_print], 1):
        print(
            f"{i:02d}) {c.start} -> {c.end} | bars={c.bars} | {c.kind} | "
            f"score={c.score:.3f} | adj={c.adjacent_overlap:.3f} | body_adj={c.body_overlap:.3f} | "
            f"eff={c.directional_efficiency:.3f} | range_units={c.range_units:.2f} | "
            f"slope_u/bar={c.slope_units_per_bar:.3f} | new_ext={c.new_extreme_rate:.3f} | "
            f"H={c.range_high:.2f} L={c.range_low:.2f}"
        )
    if len(candidates) > max_print:
        print(f"... {len(candidates) - max_print} more not printed. Use --max-print to show more.")

def main() -> None:
    ohlc_dir = Path(__file__).resolve().parent / "ohlc"

    while True:
        try:
            symbol = input("Symbol: ").strip()
            exchange = input("Exchange: ").strip()
            timeframe = input("Timeframe: ").strip()
            start_datetime = input("Start datetime: ").strip()
            end_datetime = input("End datetime: ").strip()

            instrument = symbol + "-" + exchange + "-" + timeframe + ".csv"
            ohlc_path = ohlc_dir / instrument

            if ohlc_path.exists():
                break

            print(f"File not found: {ohlc_path}")
        except Exception as e:
            print(f"Error: {e}")

    datetime_col = None
    message_col = "Message"
    source_tz_if_naive = "UTC"
    target_tz = "Asia/Bangkok"
    round_to_minute = False
    resample_rule = None
    min_bars = 3
    max_bars = 60
    step = 1
    anchor_pivots = True
    pivot_left = 3
    pivot_right = 3
    mode = "both"
    selection = "weighted"
    prefer = "balanced"
    min_gap_bars = 0
    top_n = 200
    max_print = 80
    save_csv = None

    df = load_ohlc_file(
        ohlc_path,
        datetime_col=datetime_col,
        message_col=message_col,
        source_tz_if_naive=source_tz_if_naive,
        target_tz=target_tz,
        round_to_minute=round_to_minute,
    )
    df = resample_ohlc(df, resample_rule)

    try:
        df = slice_ohlc(df, start_datetime, end_datetime)
    except ValueError as e:
        print(e)
        return

    raw = scan_ttr_candidates(
        df,
        min_bars,
        max_bars,
        step,
        mode,
        anchor_pivots=anchor_pivots,
        pivot_left=pivot_left,
        pivot_right=pivot_right,
    )
    if selection == "weighted":
        selected = select_weighted_nonoverlap(raw, min_gap_bars=min_gap_bars, prefer=prefer)
        # Keep display reasonable but preserve time order.
        if len(selected) > top_n:
            selected = sorted(selected, key=lambda c: candidate_weight(c, prefer), reverse=True)[:top_n]
            selected = sorted(selected, key=lambda c: c.start_i)
    else:
        selected = sorted(raw, key=lambda c: (c.score, c.bars), reverse=True)[:top_n]
        selected = sorted(selected, key=lambda c: c.start_i)

    print(f"version={VERSION}")
    print(f"bars_loaded={len(df)} start={df.index[0]} end={df.index[-1]} resample={resample_rule}")
    print(f"mode={mode} selection={selection} prefer={prefer} anchor_pivots={anchor_pivots}")
    print(f"raw_candidates={len(raw)} selected_candidates={len(selected)}")
    print_candidates(selected, max_print)
    if save_csv:
        out = candidates_to_df(selected)
        out.to_csv(save_csv, index=False, encoding="utf-8-sig")
        print(f"Saved: {save_csv}")


if __name__ == "__main__":
    main()
