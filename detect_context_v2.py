
"""
detect_context_v2.py

Purpose:
    Generate context-aware "query tags" from OHLC, not trade signals.
    This version avoids a binary "trading range or not" label.

Core idea:
    A market can be:
    - broad trading range
    - sloping channel inside a broad range
    - terminal breakout from that channel/range
    at the same time.

This is intended for David's retrieval/query layer, not direct trading.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Dict, Any

import numpy as np
import pandas as pd


VERSION = "2026-05-16-context-v2-channel-breakout"


def load_ohlc_file(
    input_path: str | Path,
    datetime_col: Optional[str] = None,
    message_col: str = "Message",
    target_tz: str = "Asia/Bangkok",
) -> pd.DataFrame:
    input_path = Path(input_path)

    if input_path.suffix.lower() == ".csv":
        raw = pd.read_csv(input_path)
    elif input_path.suffix.lower() in [".xlsx", ".xls"]:
        raw = pd.read_excel(input_path)
    else:
        raise ValueError("Supported input files: .csv, .xlsx, .xls")

    if datetime_col is None:
        # Prefer common datetime column names, else first column.
        for c in ["Datetime", "Date", "date", "datetime", "time", "Time"]:
            if c in raw.columns:
                datetime_col = c
                break
        if datetime_col is None:
            datetime_col = raw.columns[0]

    dt = pd.to_datetime(raw[datetime_col], errors="coerce")
    if dt.isna().any():
        raise ValueError(f"Could not parse {dt.isna().sum()} datetime rows from column {datetime_col}")

    idx = pd.DatetimeIndex(dt)
    if idx.tz is not None:
        idx = idx.tz_convert(target_tz).tz_localize(None)

    raw = raw.copy()
    raw.index = idx
    raw = raw.sort_index()

    if message_col in raw.columns:
        parts = raw[message_col].astype(str).str.split(",", expand=True)
        if parts.shape[1] < 4:
            raise ValueError(f"{message_col} must contain open,high,low,close")
        out = parts.iloc[:, :4].astype(float)
        out.columns = ["open", "high", "low", "close"]
        out.index = raw.index
        return out.dropna()

    lower_map = {str(c).strip().lower(): c for c in raw.columns}
    required = ["open", "high", "low", "close"]
    missing = [c for c in required if c not in lower_map]
    if missing:
        raise ValueError(f"Missing OHLC columns: {missing}. Found columns: {list(raw.columns)}")

    out = raw[[lower_map[c] for c in required]].astype(float)
    out.columns = required
    return out.dropna()


def slice_ohlc(df: pd.DataFrame, start: Optional[str], end: Optional[str]) -> pd.DataFrame:
    out = df.copy()
    if start:
        out = out[out.index >= pd.Timestamp(start)]
    if end:
        out = out[out.index <= pd.Timestamp(end)]
    if out.empty:
        raise ValueError(f"No rows found for start={start}, end={end}")
    return out


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def directional_efficiency(df: pd.DataFrame) -> float:
    if len(df) < 2:
        return 0.0
    net = abs(float(df["close"].iloc[-1] - df["close"].iloc[0]))
    path = float(df["close"].diff().abs().sum())
    return net / path if path > 0 else 0.0


def overlap_ratio(df: pd.DataFrame) -> float:
    if len(df) < 2:
        return 0.0

    vals = []
    for i in range(1, len(df)):
        prev = df.iloc[i - 1]
        cur = df.iloc[i]
        overlap = max(0.0, min(prev["high"], cur["high"]) - max(prev["low"], cur["low"]))
        rng = cur["high"] - cur["low"]
        if rng > 0:
            vals.append(overlap / rng)
    return float(np.mean(vals)) if vals else 0.0


def choppiness_index(df: pd.DataFrame) -> float:
    """
    Choppiness Index-like value.
    Higher = more range/choppy.
    Lower = more directional.
    This is a sensor, not a trading signal.
    """
    n = len(df)
    if n < 2:
        return 50.0
    tr_sum = float(true_range(df).dropna().sum())
    height = float(df["high"].max() - df["low"].min())
    if tr_sum <= 0 or height <= 0:
        return 50.0
    return float(100.0 * np.log10(tr_sum / height) / np.log10(n))


def fit_regression_channel(df: pd.DataFrame, width_k: float = 1.6) -> Dict[str, Any]:
    """
    Fit a simple regression channel to HLC3.
    width_k controls channel width using residual standard deviation.
    """
    if len(df) < 5:
        raise ValueError("Need at least 5 bars to fit a channel.")

    y = ((df["high"] + df["low"] + df["close"]) / 3.0).to_numpy(dtype=float)
    x = np.arange(len(y), dtype=float)

    slope, intercept = np.polyfit(x, y, 1)
    mid = slope * x + intercept
    resid = y - mid
    resid_std = float(np.std(resid, ddof=1)) if len(y) > 1 else 0.0

    ss_res = float(np.sum((y - mid) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    typical_bar_range = float((df["high"] - df["low"]).median())
    if typical_bar_range <= 0:
        typical_bar_range = float((df["high"] - df["low"]).mean())

    slope_units = float(slope / typical_bar_range) if typical_bar_range > 0 else 0.0

    return {
        "slope": float(slope),
        "intercept": float(intercept),
        "r2": float(r2),
        "resid_std": float(resid_std),
        "width_k": float(width_k),
        "typical_bar_range": float(typical_bar_range),
        "slope_units_per_bar": float(slope_units),
        "channel_top_last": float(mid[-1] + width_k * resid_std),
        "channel_bottom_last": float(mid[-1] - width_k * resid_std),
        "range_high": float(df["high"].max()),
        "range_low": float(df["low"].min()),
        "range_height": float(df["high"].max() - df["low"].min()),
        "start": str(df.index[0]),
        "end": str(df.index[-1]),
        "bars": int(len(df)),
    }


def classify_context_from_metrics(ctx: pd.DataFrame, channel: Dict[str, Any]) -> Dict[str, Any]:
    eff = directional_efficiency(ctx)
    ov = overlap_ratio(ctx)
    chop = choppiness_index(ctx)
    range_units = (
        channel["range_height"] / channel["typical_bar_range"]
        if channel["typical_bar_range"] > 0
        else 0.0
    )
    slope_units = channel["slope_units_per_bar"]

    tags = []

    # Range-like is not the same as flat. A sloping channel can be inside a broad TR.
    if ov >= 0.50 and eff <= 0.25 and range_units >= 6:
        tags.append("broad_trading_range_or_two_sided_market")
    elif ov >= 0.60 and eff <= 0.18:
        tags.append("tight_or_overlapping_range")

    if slope_units <= -0.015:
        tags.append("bear_channel_bias")
    elif slope_units >= 0.015:
        tags.append("bull_channel_bias")
    else:
        tags.append("mostly_horizontal_context")

    if chop >= 55:
        tags.append("choppy_context")
    elif chop <= 38:
        tags.append("directional_context")

    return {
        "tags": tags,
        "directional_efficiency": float(eff),
        "overlap_ratio": float(ov),
        "choppiness_index": float(chop),
        "range_units": float(range_units),
    }


def score_terminal_breakout(
    full: pd.DataFrame,
    breakout_bars: int,
    width_k: float = 1.6,
) -> Dict[str, Any]:
    """
    Try a split: prior bars = context/channel, last N bars = terminal event.
    Scores whether the terminal event is a breakout from the prior channel/range.
    """
    ctx = full.iloc[:-breakout_bars].copy()
    brk = full.iloc[-breakout_bars:].copy()

    channel = fit_regression_channel(ctx, width_k=width_k)
    n = len(ctx)
    x = np.arange(n, n + len(brk), dtype=float)
    mid = channel["slope"] * x + channel["intercept"]
    upper = mid + width_k * channel["resid_std"]
    lower = mid - width_k * channel["resid_std"]

    close = brk["close"].to_numpy(dtype=float)
    high = brk["high"].to_numpy(dtype=float)
    low = brk["low"].to_numpy(dtype=float)

    typ = channel["typical_bar_range"] if channel["typical_bar_range"] > 0 else 1.0

    below_close_ratio = float(np.mean(close < lower))
    below_low_ratio = float(np.mean(low < lower))
    above_close_ratio = float(np.mean(close > upper))
    above_high_ratio = float(np.mean(high > upper))

    down_distance_units = float(np.mean(np.maximum(0.0, lower - close) / typ))
    up_distance_units = float(np.mean(np.maximum(0.0, close - upper) / typ))

    brk_eff = directional_efficiency(brk)
    brk_net_units = float((brk["close"].iloc[-1] - brk["open"].iloc[0]) / typ)

    down_score = below_close_ratio * 2.0 + below_low_ratio + down_distance_units + max(0.0, -brk_net_units) * 0.20 + brk_eff
    up_score = above_close_ratio * 2.0 + above_high_ratio + up_distance_units + max(0.0, brk_net_units) * 0.20 + brk_eff

    if down_score >= up_score:
        direction = "down"
        score = down_score
        close_break_ratio = below_close_ratio
        wick_break_ratio = below_low_ratio
        distance_units = down_distance_units
    else:
        direction = "up"
        score = up_score
        close_break_ratio = above_close_ratio
        wick_break_ratio = above_high_ratio
        distance_units = up_distance_units

    return {
        "direction": direction,
        "score": float(score),
        "breakout_bars": int(breakout_bars),
        "close_break_ratio": float(close_break_ratio),
        "wick_break_ratio": float(wick_break_ratio),
        "avg_distance_units": float(distance_units),
        "breakout_efficiency": float(brk_eff),
        "breakout_net_units": float(brk_net_units),
        "context": classify_context_from_metrics(ctx, channel),
        "channel": channel,
        "breakout_start": str(brk.index[0]),
        "breakout_end": str(brk.index[-1]),
    }


def detect_terminal_breakout(
    df: pd.DataFrame,
    min_context_bars: int = 50,
    min_breakout_bars: int = 3,
    max_breakout_bars: int = 15,
    width_k: float = 1.6,
) -> Dict[str, Any]:
    if len(df) < min_context_bars + min_breakout_bars:
        return {
            "has_terminal_breakout": False,
            "reason": "not_enough_bars",
            "bars": len(df),
        }

    max_b = min(max_breakout_bars, len(df) - min_context_bars)
    candidates = [
        score_terminal_breakout(df, b, width_k=width_k)
        for b in range(min_breakout_bars, max_b + 1)
    ]
    best = max(candidates, key=lambda x: x["score"])

    # Conservative: require multiple closes/wicks outside channel or strong distance.
    has_breakout = (
        best["close_break_ratio"] >= 0.60
        and best["wick_break_ratio"] >= 0.60
        and best["avg_distance_units"] >= 0.75
        and abs(best["breakout_net_units"]) >= 1.5
    )

    ctx_range_height = best["channel"]["range_height"]
    ctx_range_high = best["channel"]["range_high"]
    ctx_range_low = best["channel"]["range_low"]

    if best["direction"] == "down":
        measured_move_target = ctx_range_low - ctx_range_height
        target_hit = float(df["low"].iloc[-1]) <= measured_move_target
    else:
        measured_move_target = ctx_range_high + ctx_range_height
        target_hit = float(df["high"].iloc[-1]) >= measured_move_target

    result = {
        "has_terminal_breakout": bool(has_breakout),
        "terminal_event": (
            f"{best['direction']}_breakout_from_prior_channel_or_range"
            if has_breakout
            else "no_clear_terminal_breakout"
        ),
        "query_tags": [],
        "best_split": best,
        "measured_move": {
            "basis": "prior_context_high_low_height",
            "target": float(measured_move_target),
            "target_hit_by_window_end": bool(target_hit),
        },
    }

    # Build query tags for retrieval. These are not trade decisions.
    result["query_tags"].extend(best["context"]["tags"])
    if has_breakout:
        result["query_tags"].append(result["terminal_event"])
        if result["measured_move"]["target_hit_by_window_end"]:
            result["query_tags"].append("measured_move_target_reached")
        else:
            result["query_tags"].append("measured_move_target_not_reached")
    else:
        result["query_tags"].append("no_terminal_breakout_confirmed")

    return result


def summarize_for_retrieval(result: Dict[str, Any]) -> str:
    split = result.get("best_split", {})
    ctx = split.get("context", {})
    ch = split.get("channel", {})
    tags = result.get("query_tags", [])

    parts = [
        "Market context query tags:",
        ", ".join(tags),
        f"terminal_event={result.get('terminal_event')}",
        f"context_start={ch.get('start')}",
        f"context_end={ch.get('end')}",
        f"breakout_start={split.get('breakout_start')}",
        f"breakout_end={split.get('breakout_end')}",
        f"slope_units_per_bar={ch.get('slope_units_per_bar'):.4f}",
        f"overlap_ratio={ctx.get('overlap_ratio'):.3f}",
        f"directional_efficiency={ctx.get('directional_efficiency'):.3f}",
        f"choppiness_index={ctx.get('choppiness_index'):.2f}",
        f"breakout_bars={split.get('breakout_bars')}",
        f"breakout_direction={split.get('direction')}",
        f"breakout_net_units={split.get('breakout_net_units'):.2f}",
        f"measured_move_target={result.get('measured_move', {}).get('target'):.2f}",
        f"measured_move_target_hit={result.get('measured_move', {}).get('target_hit_by_window_end')}",
    ]
    return "\n".join(parts)


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

    df = load_ohlc_file(ohlc_path, message_col="Message")

    try:
        selected = slice_ohlc(
            df,
            start=start_datetime,
            end=end_datetime,
        )
    except ValueError as e:
        print(e)
        return

    result = detect_terminal_breakout(selected)

    print(f"version={VERSION}")
    print(f"bars={len(selected)} start={selected.index[0]} end={selected.index[-1]}")
    print(summarize_for_retrieval(result))


if __name__ == "__main__":
    main()
