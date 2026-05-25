"""
detect_ttr_v3.py

Step-1 detector for TTR / overlapped-bars candidates.

Scope of this file:
- Detect overlapped-bar / TTR candidates only.
- Do NOT decide breakout, failed breakout, bull trap, bear flag, or strategy.
- Add bar-context + consecutive bar/spike awareness so trend legs are not
  swallowed into one large "range" candidate.

New in v3:
1) Bar context classification:
   bull/bear, doji, reversal bar, strong bull/bear, large breakout separator.
2) Consecutive same-color runs create anchors where sideways/overlap can start.
3) Large strong breakout bars can be treated as separators. A new TTR candidate
   starts after them, instead of including them inside the overlap range.
4) Climax-like large bars that are NOT strong breakout bars may be allowed as
   the first bar of a new overlap range.
5) Output can include overlapping candidates. We do not force every part to be
   continuous or mutually exclusive.

Supported OHLC formats:
- Datetime index + Message column containing "open,high,low,close"
- Datetime column/index + open/high/low/close columns
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, List

import numpy as np
import pandas as pd

VERSION = "2026-05-17-detect-ttr-v3-bar-context-anchors"

# -----------------------------
# Loading / normalization
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
            f"Missing={missing}. Columns={list(df.columns)}"
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
    suffix = input_path.suffix.lower()
    if suffix == ".csv":
        raw = pd.read_csv(input_path, index_col=0 if datetime_col is None else None, parse_dates=False)
    elif suffix in [".xlsx", ".xls"]:
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


def slice_ohlc(df: pd.DataFrame, start: Optional[str], end: Optional[str]) -> pd.DataFrame:
    if start or end:
        out = df.loc[start:end].copy()
    else:
        out = df.copy()
    if out.empty:
        raise ValueError(f"No OHLC rows found between start={start} and end={end}")
    return out


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if not rule:
        return df
    out = df.resample(rule).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }).dropna()
    return out

# -----------------------------
# Bar context
# -----------------------------

def add_bar_context(
    df: pd.DataFrame,
    doji_body_max: float = 0.25,
    reversal_midline: float = 0.50,
    strong_body_min: float = 0.45,
    strong_clv: float = 0.70,
    rolling_range: int = 20,
    separator_body_min: float = 0.65,
    separator_clv_extreme: float = 0.85,
    separator_range_mult: float = 1.80,
) -> pd.DataFrame:
    out = df.copy()
    rng = (out["high"] - out["low"]).replace(0, np.nan)
    body = (out["close"] - out["open"]).abs()
    out["bar_range"] = rng.fillna(0.0)
    out["body"] = body
    out["body_ratio"] = (body / rng).fillna(0.0)
    out["clv"] = ((out["close"] - out["low"]) / rng).fillna(0.5)  # 0 low, 1 high
    out["upper_tail_ratio"] = ((out["high"] - out[["open", "close"]].max(axis=1)) / rng).fillna(0.0)
    out["lower_tail_ratio"] = ((out[["open", "close"]].min(axis=1) - out["low"]) / rng).fillna(0.0)

    out["color"] = np.where(out["close"] > out["open"], "bull", np.where(out["close"] < out["open"], "bear", "doji"))
    out["is_doji"] = out["body_ratio"] <= doji_body_max
    out["is_bull_reversal"] = (out["color"] == "bull") & (~out["is_doji"]) & (out["clv"] < reversal_midline)
    out["is_bear_reversal"] = (out["color"] == "bear") & (~out["is_doji"]) & (out["clv"] > reversal_midline)
    out["is_reversal_bar"] = out["is_bull_reversal"] | out["is_bear_reversal"]

    out["is_strong_bull"] = (out["color"] == "bull") & (out["body_ratio"] >= strong_body_min) & (out["clv"] >= strong_clv)
    out["is_strong_bear"] = (out["color"] == "bear") & (out["body_ratio"] >= strong_body_min) & (out["clv"] <= 1.0 - strong_clv)

    rr_med = out["bar_range"].rolling(rolling_range, min_periods=max(3, rolling_range // 4)).median().shift(1)
    rr_med = rr_med.fillna(out["bar_range"].median())
    out["range_vs_recent_median"] = (out["bar_range"] / rr_med.replace(0, np.nan)).fillna(0.0)

    bull_sep = (
        (out["color"] == "bull")
        & (out["body_ratio"] >= separator_body_min)
        & (out["clv"] >= separator_clv_extreme)
        & (out["range_vs_recent_median"] >= separator_range_mult)
    )
    bear_sep = (
        (out["color"] == "bear")
        & (out["body_ratio"] >= separator_body_min)
        & (out["clv"] <= 1.0 - separator_clv_extreme)
        & (out["range_vs_recent_median"] >= separator_range_mult)
    )
    out["is_breakout_separator"] = bull_sep | bear_sep
    out["separator_direction"] = np.where(bull_sep, "up", np.where(bear_sep, "down", ""))

    # Large climax-like bar, but not a strong close at the extreme. This can start a new overlap range.
    out["is_climax_anchor_bar"] = (
        (out["range_vs_recent_median"] >= separator_range_mult)
        & (~out["is_breakout_separator"])
    )

    label = []
    for _, r in out.iterrows():
        if r["is_doji"]:
            label.append(f"{r['color']}_doji")
        elif r["is_bull_reversal"]:
            label.append("bull_reversal")
        elif r["is_bear_reversal"]:
            label.append("bear_reversal")
        elif r["is_strong_bull"]:
            label.append("strong_bull")
        elif r["is_strong_bear"]:
            label.append("strong_bear")
        else:
            label.append(str(r["color"]))
    out["bar_context"] = label
    return out


def find_anchor_indices(
    ctx: pd.DataFrame,
    min_run_bars: int = 3,
    include_after_separator: bool = True,
    include_climax_bar: bool = True,
) -> List[int]:
    """
    Anchor points where overlap/range may begin.

    This is intentionally primitive. It does not label a pattern.
    It only says: after consecutive one-color pressure, or after a separator,
    start looking for overlapped bars again.
    """
    n = len(ctx)
    anchors: set[int] = set()

    # Always allow first bar as an anchor.
    if n:
        anchors.add(0)

    # Anchors after strong breakout separator bars.
    if include_after_separator:
        sep_pos = np.flatnonzero(ctx["is_breakout_separator"].to_numpy())
        for p in sep_pos:
            if p + 1 < n:
                anchors.add(p + 1)

    # Climax bars that are not strong breakout separators can start overlap.
    if include_climax_bar:
        for p in np.flatnonzero(ctx["is_climax_anchor_bar"].to_numpy()):
            anchors.add(int(p))

    colors = ctx["color"].to_list()
    is_soft_turn = (ctx["is_doji"] | ctx["is_reversal_bar"]).to_numpy()

    i = 0
    while i < n:
        col = colors[i]
        if col not in ("bull", "bear"):
            i += 1
            continue
        j = i
        while j + 1 < n and colors[j + 1] == col:
            j += 1
        run_len = j - i + 1
        if run_len >= min_run_bars:
            # First doji/reversal inside the latter part of a same-color run often indicates hidden LTF reversal.
            soft_positions = [k for k in range(i + min_run_bars - 1, j + 1) if is_soft_turn[k]]
            if soft_positions:
                soft = soft_positions[0]
                # Include one bar before the visible soft turn if possible. This captures the last push/climax bar.
                anchors.add(max(i, soft - 1))
                anchors.add(soft)
            elif j + 1 < n:
                anchors.add(j + 1)
        i = j + 1

    return sorted(a for a in anchors if 0 <= a < n)

# -----------------------------
# Candidate scoring
# -----------------------------

@dataclass
class TTRCandidate:
    start: str
    end: str
    start_i: int
    end_i: int
    bars: int
    candidate_type: str
    score: float
    adjacent_overlap: float
    body_overlap: float
    directional_efficiency: float
    range_units: float
    slope_units_per_bar: float
    new_extreme_rate: float
    breakout_separators_inside: int
    anchor_reason: str
    range_high: float
    range_low: float
    range_height: float
    typical_bar_range: float


def adjacent_overlap_ratio(w: pd.DataFrame) -> float:
    vals = []
    for i in range(1, len(w)):
        prev = w.iloc[i - 1]
        cur = w.iloc[i]
        ov = max(0.0, min(prev.high, cur.high) - max(prev.low, cur.low))
        cr = cur.high - cur.low
        if cr > 0:
            vals.append(ov / cr)
    return float(np.mean(vals)) if vals else 0.0


def body_overlap_ratio(w: pd.DataFrame) -> float:
    vals = []
    for i in range(1, len(w)):
        prev = w.iloc[i - 1]
        cur = w.iloc[i]
        prev_top = max(prev.open, prev.close)
        prev_bot = min(prev.open, prev.close)
        cur_top = max(cur.open, cur.close)
        cur_bot = min(cur.open, cur.close)
        ov = max(0.0, min(prev_top, cur_top) - max(prev_bot, cur_bot))
        denom = max(cur_top - cur_bot, 1e-9)
        vals.append(min(1.0, ov / denom))
    return float(np.mean(vals)) if vals else 0.0


def directional_efficiency(w: pd.DataFrame) -> float:
    if len(w) < 2:
        return 0.0
    path = float(w["close"].diff().abs().sum())
    if path <= 0:
        return 0.0
    return float(abs(w["close"].iloc[-1] - w["close"].iloc[0]) / path)


def new_extreme_rate(w: pd.DataFrame) -> float:
    if len(w) < 2:
        return 0.0
    new_ext = 0
    running_high = float(w["high"].iloc[0])
    running_low = float(w["low"].iloc[0])
    for i in range(1, len(w)):
        hi = float(w["high"].iloc[i])
        lo = float(w["low"].iloc[i])
        if hi > running_high or lo < running_low:
            new_ext += 1
        running_high = max(running_high, hi)
        running_low = min(running_low, lo)
    return new_ext / (len(w) - 1)


def slope_units_per_bar(w: pd.DataFrame, typical: float) -> float:
    if len(w) < 2 or typical <= 0:
        return 0.0
    y = w["close"].to_numpy(dtype=float)
    x = np.arange(len(y), dtype=float)
    slope = float(np.polyfit(x, y, 1)[0])
    return slope / typical


def score_window(
    ctx: pd.DataFrame,
    start_i: int,
    end_i: int,
    max_absolute_range_units: float,
    max_stair_range_units: float,
    min_adj_overlap: float,
    max_efficiency: float,
) -> Optional[TTRCandidate]:
    w = ctx.iloc[start_i : end_i + 1]
    bars = len(w)
    if bars < 3:
        return None

    h = float(w["high"].max())
    l = float(w["low"].min())
    height = h - l
    typical = float(w["bar_range"].median())
    if typical <= 0:
        typical = float(w["bar_range"].mean())
    if typical <= 0:
        return None

    range_units = height / typical
    adj = adjacent_overlap_ratio(w)
    bod = body_overlap_ratio(w)
    eff = directional_efficiency(w)
    ner = new_extreme_rate(w)
    slope_u = slope_units_per_bar(w, typical)
    sep_inside = int(w["is_breakout_separator"].iloc[1:-1].sum()) if bars > 2 else 0

    # Strong breakout bars inside a candidate usually mean we are crossing phases.
    if sep_inside > 0:
        return None
    if adj < min_adj_overlap:
        return None
    if eff > max_efficiency:
        return None

    length_quality = min(1.0, bars / 20.0)
    compact_abs = max(0.0, 1.0 - range_units / max_absolute_range_units)
    compact_stair = max(0.0, 1.0 - range_units / max_stair_range_units)

    abs_score = (
        0.34 * adj
        + 0.22 * (1.0 - eff)
        + 0.22 * compact_abs
        + 0.10 * bod
        + 0.08 * length_quality
        + 0.04 * (1.0 - ner)
    )
    stair_score = (
        0.32 * adj
        + 0.16 * (1.0 - eff)
        + 0.14 * compact_stair
        + 0.10 * bod
        + 0.10 * length_quality
        + 0.10 * min(1.0, range_units / 5.0)
        + 0.08 * (1.0 - ner)
    )

    if range_units <= max_absolute_range_units and abs_score >= stair_score * 0.90:
        ctype = "absolute_overlap_ttr_candidate"
        score = abs_score
    elif range_units <= max_stair_range_units:
        ctype = "stair_overlap_ttr_candidate"
        score = stair_score
    else:
        return None

    return TTRCandidate(
        start=str(w.index[0]),
        end=str(w.index[-1]),
        start_i=start_i,
        end_i=end_i,
        bars=bars,
        candidate_type=ctype,
        score=float(score),
        adjacent_overlap=float(adj),
        body_overlap=float(bod),
        directional_efficiency=float(eff),
        range_units=float(range_units),
        slope_units_per_bar=float(slope_u),
        new_extreme_rate=float(ner),
        breakout_separators_inside=sep_inside,
        anchor_reason="",
        range_high=h,
        range_low=l,
        range_height=height,
        typical_bar_range=typical,
    )


def anchor_reason(ctx: pd.DataFrame, i: int) -> str:
    if i <= 0:
        return "start_of_scan"
    r = ctx.iloc[i]
    prev = ctx.iloc[i - 1]
    if bool(prev["is_breakout_separator"]):
        return f"after_{prev['separator_direction']}_breakout_separator"
    if bool(r["is_climax_anchor_bar"]):
        return "climax_anchor_bar_not_strong_breakout_close"
    if bool(r["is_doji"]):
        return "doji_after_consecutive_pressure"
    if bool(r["is_reversal_bar"]):
        return "reversal_bar_after_consecutive_pressure"
    return "anchor_after_consecutive_pressure"


def detect_ttr_candidates(
    df: pd.DataFrame,
    min_bars: int = 3,
    max_bars: int = 60,
    min_run_bars: int = 3,
    min_adj_overlap: float = 0.48,
    max_efficiency: float = 0.72,
    max_absolute_range_units: float = 5.0,
    max_stair_range_units: float = 6.2,
    keep_top_per_anchor: int = 2,
    allow_anchor_overlap: bool = True,
) -> tuple[pd.DataFrame, list[TTRCandidate], list[int]]:
    ctx = add_bar_context(df)
    anchors = find_anchor_indices(ctx, min_run_bars=min_run_bars)

    # Build natural boundaries: next anchor or separator. This prevents one large low-efficiency
    # window from swallowing multiple TTR pieces.
    separators = set(np.flatnonzero(ctx["is_breakout_separator"].to_numpy()).tolist())
    candidates: list[TTRCandidate] = []

    for ai, start_i in enumerate(anchors):
        # Find next separator strictly after start_i.
        next_sep = min([p for p in separators if p > start_i], default=len(ctx))
        next_anchor = anchors[ai + 1] if ai + 1 < len(anchors) else len(ctx)

        if allow_anchor_overlap:
            # Use next strong breakout separator as hard boundary; anchors can overlap.
            hard_end = min(len(ctx) - 1, next_sep - 1)
        else:
            hard_end = min(len(ctx) - 1, next_sep - 1, next_anchor - 1)

        local: list[TTRCandidate] = []
        for end_i in range(start_i + min_bars - 1, min(start_i + max_bars - 1, hard_end) + 1):
            cand = score_window(
                ctx,
                start_i,
                end_i,
                max_absolute_range_units=max_absolute_range_units,
                max_stair_range_units=max_stair_range_units,
                min_adj_overlap=min_adj_overlap,
                max_efficiency=max_efficiency,
            )
            if cand is not None:
                cand.anchor_reason = anchor_reason(ctx, start_i)
                local.append(cand)

        if not local:
            continue

        # Prefer candidate endings before the next anchor when possible, but allow the longer
        # same-phase candidate when it is materially better.
        before_next_anchor = [c for c in local if c.end_i <= next_anchor - 1]
        pool = before_next_anchor if before_next_anchor else local

        # Do not always pick the mathematically best score, because that tends to prefer tiny 3-4 bar clusters.
        # First choose the best longer candidate (>=6 bars if available), then one compact micro candidate.
        longer = [c for c in pool if c.bars >= 6]
        chosen: list[TTRCandidate] = []
        if longer:
            chosen.append(max(longer, key=lambda c: (c.score + min(c.bars, 30) * 0.003, c.bars)))
        else:
            chosen.append(max(pool, key=lambda c: c.score))

        if keep_top_per_anchor > 1:
            rest = [c for c in pool if c is not chosen[0]]
            if rest:
                # Add a second candidate only if it is meaningfully different in end point.
                rest = sorted(rest, key=lambda c: c.score, reverse=True)
                for r in rest:
                    if abs(r.end_i - chosen[0].end_i) >= 3:
                        chosen.append(r)
                        break

        candidates.extend(chosen[:keep_top_per_anchor])

    # De-duplicate very similar candidates of same type and same start.
    out: list[TTRCandidate] = []
    seen = set()
    for c in sorted(candidates, key=lambda x: (x.start_i, x.end_i, -x.score)):
        key = (c.start_i, c.end_i, c.candidate_type)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)

    return ctx, out, anchors

# -----------------------------
# Main
# -----------------------------

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
    min_run_bars = 3
    min_adj_overlap = 0.48
    max_efficiency = 0.72
    max_absolute_range_units = 5.0
    max_stair_range_units = 6.2
    keep_top_per_anchor = 1
    allow_anchor_overlap = True
    max_print = 80
    show_bars = False
    json_output = False
    save_csv = None
    save_bars_csv = None

    df = load_ohlc_file(
        ohlc_path,
        datetime_col=datetime_col,
        message_col=message_col,
        source_tz_if_naive=source_tz_if_naive,
        target_tz=target_tz,
        round_to_minute=round_to_minute,
    )
    df = resample_ohlc(df, resample_rule) if resample_rule else df

    try:
        df = slice_ohlc(df, start_datetime, end_datetime)
    except ValueError as e:
        print(e)
        return

    ctx, candidates, anchors = detect_ttr_candidates(
        df,
        min_bars=min_bars,
        max_bars=max_bars,
        min_run_bars=min_run_bars,
        min_adj_overlap=min_adj_overlap,
        max_efficiency=max_efficiency,
        max_absolute_range_units=max_absolute_range_units,
        max_stair_range_units=max_stair_range_units,
        keep_top_per_anchor=max(1, keep_top_per_anchor),
        allow_anchor_overlap=allow_anchor_overlap,
    )

    if save_bars_csv:
        cols = [
            "open", "high", "low", "close", "bar_context", "color", "body_ratio", "clv",
            "lower_tail_ratio", "upper_tail_ratio", "range_vs_recent_median",
            "is_breakout_separator", "separator_direction", "is_climax_anchor_bar"
        ]
        ctx[cols].to_csv(save_bars_csv, encoding="utf-8-sig")

    rows = [asdict(c) for c in candidates]
    if save_csv:
        pd.DataFrame(rows).to_csv(save_csv, index=False, encoding="utf-8-sig")

    if json_output:
        print(json.dumps({
            "version": VERSION,
            "bars_loaded": len(df),
            "start": str(df.index[0]),
            "end": str(df.index[-1]),
            "anchors": [str(ctx.index[i]) for i in anchors],
            "candidates": rows,
        }, indent=2, ensure_ascii=False))
        return

    print(f"version={VERSION}")
    print(f"bars_loaded={len(df)} start={df.index[0]} end={df.index[-1]} resample={resample_rule}")
    print(f"anchors={len(anchors)} candidates={len(candidates)}")
    if anchors:
        print("Anchor times:")
        print(", ".join(str(ctx.index[i]) for i in anchors[:80]))

    print(f"Detected TTR / overlap range candidates: {len(candidates)}")
    for n, c in enumerate(candidates[:max_print], 1):
        print(
            f"{n:02d}) {c.start} -> {c.end} | bars={c.bars} | {c.candidate_type} "
            f"| score={c.score:.3f} | adj={c.adjacent_overlap:.3f} | body_adj={c.body_overlap:.3f} "
            f"| eff={c.directional_efficiency:.3f} | range_units={c.range_units:.2f} "
            f"| slope_u/bar={c.slope_units_per_bar:.3f} | new_ext={c.new_extreme_rate:.3f} "
            f"| anchor={c.anchor_reason} | H={c.range_high:.2f} L={c.range_low:.2f}"
        )

    if show_bars:
        print("\nBar context:")
        display_cols = ["open", "high", "low", "close", "bar_context", "body_ratio", "clv", "range_vs_recent_median", "is_breakout_separator", "is_climax_anchor_bar"]
        print(ctx[display_cols].to_string())


if __name__ == "__main__":
    main()
