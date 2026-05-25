#!/usr/bin/env python3
"""
detect_htf_overlap_context.py
version=2026-05-16-htf-overlap-context-v1

Purpose
-------
Detect higher-timeframe overlapping-bar context / trading-range context from OHLC.
This is NOT a trade signal generator. It is a context/query sensor for David.

Core idea
---------
Do not decide only from the latest bars. First detect whether the higher timeframe
is dominated by overlapping bars and low directional efficiency. Then expose:
- range high / range low
- upper/middle/lower thirds
- last close location
- channel bias inside the range
- whether a real breakout from the whole HTF range is confirmed

Input supported
---------------
1) CSV with columns: Date, Message where Message = "open,high,low,close"
2) CSV with columns: Datetime/Open/High/Low/Close or Date/open/high/low/close

Examples
--------
py detect_htf_overlap_context.py ^
  --input "XAUUSD-GO Markets-15m.csv" ^
  --start "2026-05-13 07:00" ^
  --end "2026-05-14 20:00" ^
  --window-bars 40

If you feed 1m data and want HTF detection:
py detect_htf_overlap_context.py ^
  --input "XAUUSD-GO Markets-1m.csv" ^
  --start "2026-05-13 07:00" ^
  --end "2026-05-14 20:00" ^
  --resample 15min ^
  --window-bars 40
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

VERSION = "2026-05-16-htf-overlap-context-v1"


# -----------------------------
# Loading / normalization
# -----------------------------


def _strip_tz_index(index: pd.DatetimeIndex, target_tz: str = "Asia/Bangkok") -> pd.DatetimeIndex:
    if index.tz is not None:
        return index.tz_convert(target_tz).tz_localize(None)
    return index


def load_ohlc_csv(path: str | Path, tz: str = "Asia/Bangkok") -> pd.DataFrame:
    path = Path(path)
    raw = pd.read_csv(path)
    cols_lower = {c.lower().strip(): c for c in raw.columns}

    # Format: Date, Message = "open,high,low,close"
    if "message" in cols_lower:
        msg_col = cols_lower["message"]
        dt_col = None
        for c in ["date", "datetime", "time", "timestamp"]:
            if c in cols_lower:
                dt_col = cols_lower[c]
                break
        if dt_col is None:
            dt_col = raw.columns[0]

        dt = pd.to_datetime(raw[dt_col], errors="coerce")
        parts = raw[msg_col].astype(str).str.split(",", expand=True)
        if parts.shape[1] < 4:
            raise ValueError("Message column must contain open,high,low,close")
        ohlc = parts.iloc[:, :4].astype(float)
        ohlc.columns = ["open", "high", "low", "close"]
        df = ohlc.copy()
        df.index = pd.DatetimeIndex(dt)
        df.index = _strip_tz_index(df.index, tz)
        return df.dropna().sort_index()

    # Format: datetime + OHLC columns
    dt_col = None
    for c in ["datetime", "date", "time", "timestamp"]:
        if c in cols_lower:
            dt_col = cols_lower[c]
            break
    if dt_col is None:
        dt_col = raw.columns[0]

    rename = {}
    for want in ["open", "high", "low", "close"]:
        if want in cols_lower:
            rename[cols_lower[want]] = want
        elif want.capitalize() in raw.columns:
            rename[want.capitalize()] = want

    df = raw.rename(columns=rename)
    missing = [c for c in ["open", "high", "low", "close"] if c not in df.columns]
    if missing:
        raise ValueError(f"Missing OHLC columns: {missing}")

    dt = pd.to_datetime(df[dt_col], errors="coerce")
    out = df[["open", "high", "low", "close"]].astype(float).copy()
    out.index = pd.DatetimeIndex(dt)
    out.index = _strip_tz_index(out.index, tz)
    return out.dropna().sort_index()


def resample_ohlc(df: pd.DataFrame, rule: str | None) -> pd.DataFrame:
    if not rule:
        return df
    return (
        df.resample(rule)
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
    )


# -----------------------------
# Metrics
# -----------------------------


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    # Simple ATR is enough for context measurement.
    return true_range(df).rolling(period, min_periods=max(2, period // 2)).mean()


def adjacent_overlap_ratio(seg: pd.DataFrame) -> float:
    vals = []
    for i in range(1, len(seg)):
        a = seg.iloc[i - 1]
        b = seg.iloc[i]
        overlap = max(0.0, min(a.high, b.high) - max(a.low, b.low))
        denom = max(1e-9, b.high - b.low)
        vals.append(overlap / denom)
    return float(np.mean(vals)) if vals else 0.0


def body_overlap_ratio(seg: pd.DataFrame) -> float:
    vals = []
    for i in range(1, len(seg)):
        a = seg.iloc[i - 1]
        b = seg.iloc[i]
        a_hi, a_lo = max(a.open, a.close), min(a.open, a.close)
        b_hi, b_lo = max(b.open, b.close), min(b.open, b.close)
        overlap = max(0.0, min(a_hi, b_hi) - max(a_lo, b_lo))
        denom = max(1e-9, b_hi - b_lo)
        vals.append(min(1.0, overlap / denom))
    return float(np.mean(vals)) if vals else 0.0


def directional_efficiency(seg: pd.DataFrame) -> float:
    if len(seg) < 2:
        return 0.0
    net = abs(float(seg.close.iloc[-1] - seg.close.iloc[0]))
    path = float(seg.close.diff().abs().sum())
    return 0.0 if path <= 0 else net / path


def linreg_slope_units(seg: pd.DataFrame, unit: float) -> float:
    if len(seg) < 3 or unit <= 0:
        return 0.0
    x = np.arange(len(seg), dtype=float)
    y = seg.close.to_numpy(dtype=float)
    slope = np.polyfit(x, y, 1)[0]
    return float(slope / unit)


def close_location_in_range(seg: pd.DataFrame, value: float | None = None) -> float:
    hi = float(seg.high.max())
    lo = float(seg.low.min())
    if value is None:
        value = float(seg.close.iloc[-1])
    if hi <= lo:
        return 0.5
    return float((value - lo) / (hi - lo))


def score_overlap_context(metrics: dict[str, float]) -> float:
    """
    Score 0-100. Designed to prefer:
    - high adjacent overlap
    - low directional efficiency
    - reasonable or broad range width relative to ATR

    This score does NOT mean profitable trading range. It means HTF bars are
    overlapping / two-sided enough to be a context candidate.
    """
    overlap = metrics["adjacent_overlap"]
    eff = metrics["directional_efficiency"]
    width_units = metrics["range_height_atr_units"]

    overlap_score = np.clip((overlap - 0.35) / 0.35, 0, 1)  # 0.35->0, 0.70->1
    ineff_score = np.clip((0.35 - eff) / 0.35, 0, 1)        # 0.35->0, 0.00->1

    # Width: tight to broad is acceptable. Extremely huge windows are still allowed
    # but not allowed to dominate the score. The point is context, not trade signal.
    if width_units <= 2.5:
        width_score = 0.85
    elif width_units <= 8.0:
        width_score = 1.0
    elif width_units <= 14.0:
        width_score = 0.85
    else:
        width_score = 0.65

    score = 100.0 * (0.45 * overlap_score + 0.45 * ineff_score + 0.10 * width_score)
    return float(np.clip(score, 0, 100))


def classify_width(width_units: float) -> str:
    if width_units <= 2.5:
        return "tight_overlap_range"
    if width_units <= 8.0:
        return "broad_overlap_range"
    if width_units <= 14.0:
        return "very_broad_overlap_range"
    return "extremely_broad_two_sided_context"


def classify_channel_bias(slope_units: float, eff: float) -> str:
    # Even inside overlap context, slope can indicate bull/bear channel bias.
    if slope_units >= 0.035:
        return "bull_channel_bias"
    if slope_units <= -0.035:
        return "bear_channel_bias"
    return "flat_trading_range_bias"


@dataclass
class OverlapContext:
    start: str
    end: str
    bars: int
    range_high: float
    range_low: float
    range_height: float
    atr_median: float
    range_height_atr_units: float
    adjacent_overlap: float
    body_overlap: float
    directional_efficiency: float
    slope_units_per_bar: float
    score: float
    context_type: str
    channel_bias: str
    last_close: float
    last_close_location: float
    location_zone: str
    upper_third: float
    middle: float
    lower_third: float


def compute_context(seg: pd.DataFrame, atr_series: pd.Series | None = None) -> OverlapContext:
    if atr_series is None:
        atr_series = atr(seg)
    atr_seg = atr_series.reindex(seg.index).dropna()
    unit = float(atr_seg.median()) if len(atr_seg) else float((seg.high - seg.low).median())
    unit = max(unit, 1e-9)

    hi = float(seg.high.max())
    lo = float(seg.low.min())
    height = hi - lo
    width_units = height / unit
    adj = adjacent_overlap_ratio(seg)
    bod = body_overlap_ratio(seg)
    eff = directional_efficiency(seg)
    slope = linreg_slope_units(seg, unit)
    loc = close_location_in_range(seg)

    if loc >= 2 / 3:
        zone = "top_third"
    elif loc <= 1 / 3:
        zone = "bottom_third"
    else:
        zone = "middle_third"

    metrics = {
        "adjacent_overlap": adj,
        "directional_efficiency": eff,
        "range_height_atr_units": width_units,
    }
    score = score_overlap_context(metrics)

    return OverlapContext(
        start=str(seg.index[0]),
        end=str(seg.index[-1]),
        bars=int(len(seg)),
        range_high=round(hi, 5),
        range_low=round(lo, 5),
        range_height=round(height, 5),
        atr_median=round(unit, 5),
        range_height_atr_units=round(width_units, 3),
        adjacent_overlap=round(adj, 3),
        body_overlap=round(bod, 3),
        directional_efficiency=round(eff, 3),
        slope_units_per_bar=round(slope, 4),
        score=round(score, 1),
        context_type=classify_width(width_units),
        channel_bias=classify_channel_bias(slope, eff),
        last_close=round(float(seg.close.iloc[-1]), 5),
        last_close_location=round(loc, 3),
        location_zone=zone,
        upper_third=round(lo + height * 2 / 3, 5),
        middle=round(lo + height * 0.5, 5),
        lower_third=round(lo + height / 3, 5),
    )


def find_best_rolling_contexts(df: pd.DataFrame, window_bars: int, top_n: int = 5) -> list[OverlapContext]:
    if len(df) < window_bars:
        return []
    atr_series = atr(df)
    out: list[OverlapContext] = []
    for end_i in range(window_bars, len(df) + 1):
        seg = df.iloc[end_i - window_bars : end_i]
        out.append(compute_context(seg, atr_series))
    out.sort(key=lambda x: x.score, reverse=True)
    return out[:top_n]


def breakout_status_after_context(
    df_all: pd.DataFrame,
    ctx: OverlapContext,
    buffer_atr_units: float = 0.35,
    min_follow_bars: int = 2,
) -> dict[str, Any]:
    """
    Confirm breakout only when bars AFTER the context close beyond whole HTF range.
    If there are no bars after context end, returns no_future_bars_to_confirm.
    """
    end_ts = pd.Timestamp(ctx.end)
    future = df_all.loc[df_all.index > end_ts]
    if future.empty:
        return {"breakout_status": "no_future_bars_to_confirm"}

    buffer = ctx.atr_median * buffer_atr_units
    up_level = ctx.range_high + buffer
    down_level = ctx.range_low - buffer

    up_hits = future[future.close > up_level]
    down_hits = future[future.close < down_level]

    candidates = []
    if not up_hits.empty:
        first = up_hits.index[0]
        follow = future.loc[first:].head(max(1, min_follow_bars))
        candidates.append((first, "up", follow))
    if not down_hits.empty:
        first = down_hits.index[0]
        follow = future.loc[first:].head(max(1, min_follow_bars))
        candidates.append((first, "down", follow))

    if not candidates:
        return {
            "breakout_status": "inside_htf_range_or_no_confirmed_breakout",
            "up_breakout_level": round(up_level, 5),
            "down_breakout_level": round(down_level, 5),
        }

    first, direction, follow = sorted(candidates, key=lambda x: x[0])[0]
    if direction == "up":
        follow_count = int((follow.close > ctx.range_high).sum())
    else:
        follow_count = int((follow.close < ctx.range_low).sum())

    confirmed = follow_count >= min_follow_bars
    return {
        "breakout_status": "confirmed_breakout" if confirmed else "breakout_attempt_pending_followthrough",
        "direction": direction,
        "breakout_time": str(first),
        "followthrough_bars": follow_count,
        "up_breakout_level": round(up_level, 5),
        "down_breakout_level": round(down_level, 5),
    }


def make_query_tags(ctx: OverlapContext, breakout: dict[str, Any]) -> list[str]:
    tags = [ctx.context_type, ctx.channel_bias, f"location_{ctx.location_zone}"]

    if ctx.adjacent_overlap >= 0.55 and ctx.directional_efficiency <= 0.25:
        tags.append("overlapping_bars_context")
    if ctx.location_zone == "top_third":
        tags.append("sell_high_area_if_reversal_signal_appears")
        tags.append("beware_upside_breakout")
    elif ctx.location_zone == "bottom_third":
        tags.append("buy_low_area_if_reversal_signal_appears")
        tags.append("beware_downside_breakout")
    else:
        tags.append("middle_of_range_reduce_directional_assumption")

    status = breakout.get("breakout_status")
    if status == "confirmed_breakout":
        tags.append(f"confirmed_{breakout.get('direction')}_breakout_from_htf_range")
    elif status == "breakout_attempt_pending_followthrough":
        tags.append(f"{breakout.get('direction')}_breakout_attempt_pending_followthrough")
    else:
        tags.append("no_confirmed_htf_range_breakout")

    return tags


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

    resample_rule = None
    window_bars = 40
    top_n = 5
    json_output = False

    df = load_ohlc_csv(ohlc_path)
    df = resample_ohlc(df, resample_rule)

    try:
        df = df.loc[pd.Timestamp(start_datetime) : pd.Timestamp(end_datetime)].copy()
    except ValueError as e:
        print(e)
        return

    if df.empty:
        print("No bars in selected time range.")
        return

    selected_ctx = compute_context(df)
    best = find_best_rolling_contexts(df, window_bars=window_bars, top_n=top_n)

    # For user's HTF purpose, the selected window itself is often the macro context.
    # Best rolling windows are supporting evidence / sub-contexts.
    breakout = breakout_status_after_context(df, selected_ctx)
    tags = make_query_tags(selected_ctx, breakout)

    result = {
        "version": VERSION,
        "bars": len(df),
        "selected_context": asdict(selected_ctx),
        "breakout_after_selected_context": breakout,
        "query_tags": tags,
        "best_rolling_overlap_contexts": [asdict(x) for x in best],
    }

    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    print(f"version={VERSION}")
    print(f"bars={len(df)} start={df.index[0]} end={df.index[-1]}")
    print("\nSelected HTF overlap context:")
    for k, v in asdict(selected_ctx).items():
        print(f"{k}={v}")

    print("\nBreakout after selected context:")
    for k, v in breakout.items():
        print(f"{k}={v}")

    print("\nQuery tags:")
    print(", ".join(tags))

    print(f"\nTop {len(best)} rolling overlap windows ({window_bars} bars):")
    for i, c in enumerate(best, 1):
        print(
            f"{i}. {c.start} -> {c.end} | score={c.score} | "
            f"{c.context_type}, {c.channel_bias}, loc={c.location_zone}, "
            f"range=({c.range_low}-{c.range_high})"
        )


if __name__ == "__main__":
    main()
