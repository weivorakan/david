X# -*- coding: utf-8 -*-
"""
detect_ttr_basic.py

Back-to-basics bar-context builder for OHLC price action work.

Goal:
1) Read OHLC data.
2) Save a new CSV with original OHLC + simple bar context columns.
3) Detect consecutive same-direction bar runs.
4) Detect very simple local overlap clusters from nearby highs/lows.

This version intentionally avoids complex scoring such as weighted score,
adj/body_adj combined score, directional-efficiency score, and slope score.
It does NOT decide breakout success/failure, trap, or strategy.

Version: 2026-05-18-detect-ttr-basic-v1
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

VERSION = "2026-05-18-detect-ttr-basic-v1"


def normalize_ohlc_columns(df: pd.DataFrame, message_col: str = "Message") -> pd.DataFrame:
    df = df.copy()
    if message_col in df.columns:
        ohlc = df[message_col].astype(str).str.split(",", expand=True)
        if ohlc.shape[1] < 4:
            raise ValueError(f"Column '{message_col}' must contain open,high,low,close")
        df[["open", "high", "low", "close"]] = ohlc.iloc[:, :4].astype(float)
        return df[["open", "high", "low", "close"]].astype(float)

    lower_map = {str(c).strip().lower(): c for c in df.columns}
    required = ["open", "high", "low", "close"]
    missing = [c for c in required if c not in lower_map]
    if missing:
        raise ValueError(
            "Expected either Message='open,high,low,close' or columns open/high/low/close. "
            f"Missing: {missing}. Columns found: {list(df.columns)}"
        )
    out = df[[lower_map[c] for c in required]].copy()
    out.columns = required
    return out.astype(float)


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
            raise ValueError(f"datetime_col '{datetime_col}' was not found. Columns: {list(df.columns)}")
        dt = pd.to_datetime(df[datetime_col], errors="coerce")
        df = df.drop(columns=[datetime_col])
    else:
        dt = pd.to_datetime(df.index, errors="coerce")

    if dt.isna().any():
        raise ValueError(f"Datetime parsing failed for {int(dt.isna().sum())} row(s).")

    dt_index = pd.DatetimeIndex(dt)
    if dt_index.tz is None:
        dt_index = dt_index.tz_localize(source_tz_if_naive).tz_convert(target_tz)
    else:
        dt_index = dt_index.tz_convert(target_tz)
    dt_index = dt_index.tz_localize(None)

    if round_to_minute:
        dt_index = dt_index.round("min")

    df.index = dt_index
    return df.sort_index()


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
        df = df.loc[start:end].copy()
    if df.empty:
        raise ValueError(f"No OHLC rows found between start={start} and end={end}")
    return df


def resample_ohlc(df: pd.DataFrame, rule: Optional[str]) -> pd.DataFrame:
    if not rule:
        return df
    return df.resample(rule).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    ).dropna()


def add_bar_context(
    df: pd.DataFrame,
    doji_body_ratio: float = 0.25,
    strong_body_ratio: float = 0.55,
    close_near_extreme: float = 0.70,
    tail_ratio: float = 0.35,
    breakout_lookback: int = 1,
) -> pd.DataFrame:
    """Add simple, inspectable bar context columns."""
    out = df.copy()
    rng = (out["high"] - out["low"]).replace(0, np.nan)
    body = (out["close"] - out["open"]).abs()

    out["bar_range"] = rng
    out["body"] = body
    out["body_ratio"] = (body / rng).fillna(0.0)
    out["upper_tail"] = (out["high"] - out[["open", "close"]].max(axis=1)).clip(lower=0)
    out["lower_tail"] = (out[["open", "close"]].min(axis=1) - out["low"]).clip(lower=0)
    out["upper_tail_ratio"] = (out["upper_tail"] / rng).fillna(0.0)
    out["lower_tail_ratio"] = (out["lower_tail"] / rng).fillna(0.0)
    out["close_location"] = ((out["close"] - out["low"]) / rng).fillna(0.5)

    out["bull_bar"] = out["close"] > out["open"]
    out["bear_bar"] = out["close"] < out["open"]
    out["doji_bar"] = out["body_ratio"] <= doji_body_ratio
    out["bar_direction"] = np.where(out["bull_bar"], "bull", np.where(out["bear_bar"], "bear", "doji"))

    prev_high = out["high"].shift(1)
    prev_low = out["low"].shift(1)
    out["inside_bar"] = (out["high"] <= prev_high) & (out["low"] >= prev_low)
    out["outside_bar"] = (out["high"] >= prev_high) & (out["low"] <= prev_low)
    out["buy_trigger_prev_high"] = out["high"] > prev_high
    out["sell_trigger_prev_low"] = out["low"] < prev_low
    out["close_above_prev_high"] = out["close"] > prev_high
    out["close_below_prev_low"] = out["close"] < prev_low

    overlap_high = np.minimum(out["high"], prev_high)
    overlap_low = np.maximum(out["low"], prev_low)
    out["overlaps_prev_bar"] = (overlap_high >= overlap_low).fillna(False)

    if breakout_lookback <= 1:
        prior_high = prev_high
        prior_low = prev_low
    else:
        prior_high = out["high"].shift(1).rolling(breakout_lookback, min_periods=1).max()
        prior_low = out["low"].shift(1).rolling(breakout_lookback, min_periods=1).min()

    out["break_above_prior_high"] = out["high"] > prior_high
    out["break_below_prior_low"] = out["low"] < prior_low
    out["close_above_prior_high"] = out["close"] > prior_high
    out["close_below_prior_low"] = out["close"] < prior_low

    out["strong_bull_bar"] = out["bull_bar"] & (out["body_ratio"] >= strong_body_ratio) & (out["close_location"] >= close_near_extreme)
    out["strong_bear_bar"] = out["bear_bar"] & (out["body_ratio"] >= strong_body_ratio) & (out["close_location"] <= (1.0 - close_near_extreme))

    out["bull_reversal_bar"] = (out["lower_tail_ratio"] >= tail_ratio) & (out["close_location"] >= 0.50)
    out["bear_reversal_bar"] = (out["upper_tail_ratio"] >= tail_ratio) & (out["close_location"] <= 0.50)
    out["bull_breakout_bar"] = out["strong_bull_bar"] & out["close_above_prior_high"]
    out["bear_breakout_bar"] = out["strong_bear_bar"] & out["close_below_prior_low"]

    tags = []
    for _, r in out.iterrows():
        t = []
        if r["bull_bar"]:
            t.append("bull")
        elif r["bear_bar"]:
            t.append("bear")
        else:
            t.append("doji")
        if r["doji_bar"]:
            t.append("doji")
        if r["inside_bar"]:
            t.append("inside")
        if r["outside_bar"]:
            t.append("outside")
        if r["bull_reversal_bar"]:
            t.append("bull_reversal")
        if r["bear_reversal_bar"]:
            t.append("bear_reversal")
        if r["bull_breakout_bar"]:
            t.append("bull_breakout_bar")
        if r["bear_breakout_bar"]:
            t.append("bear_breakout_bar")
        if r["buy_trigger_prev_high"]:
            t.append("buy_trigger")
        if r["sell_trigger_prev_low"]:
            t.append("sell_trigger")
        tags.append("|".join(t))
    out["bar_context"] = tags
    return out


def detect_consecutive_runs(ctx: pd.DataFrame, min_run: int = 3, include_doji_as_break: bool = True) -> pd.DataFrame:
    rows = []
    n = len(ctx)
    dirs = ctx["bar_direction"].tolist()
    times = list(ctx.index)
    i = 0
    while i < n:
        d = dirs[i]
        if d == "doji":
            i += 1
            continue
        j = i + 1
        while j < n:
            if dirs[j] == d:
                j += 1
                continue
            if dirs[j] == "doji" and not include_doji_as_break:
                j += 1
                continue
            break
        length = j - i
        if length >= min_run:
            part = ctx.iloc[i:j]
            rows.append({
                "start": times[i],
                "end": times[j - 1],
                "bars": length,
                "direction": d,
                "high": float(part["high"].max()),
                "low": float(part["low"].min()),
                "close_start": float(part["close"].iloc[0]),
                "close_end": float(part["close"].iloc[-1]),
                "next_bar": times[j] if j < n else pd.NaT,
                "next_bar_direction": dirs[j] if j < n else None,
            })
        i = j
    return pd.DataFrame(rows)


def local_overlap_cluster_from_anchor(ctx: pd.DataFrame, anchor_idx: int, lookahead: int = 12, min_bars: int = 3) -> Optional[dict]:
    n = len(ctx)
    if anchor_idx >= n:
        return None
    start = anchor_idx
    end = anchor_idx
    env_high = float(ctx["high"].iloc[start])
    env_low = float(ctx["low"].iloc[start])
    outside_count = 0
    max_end = min(n - 1, start + lookahead)
    for k in range(start + 1, max_end + 1):
        h = float(ctx["high"].iloc[k])
        l = float(ctx["low"].iloc[k])
        overlaps_envelope = (h >= env_low) and (l <= env_high)
        inside_or_outside = bool(ctx["inside_bar"].iloc[k]) or bool(ctx["outside_bar"].iloc[k])
        if overlaps_envelope or inside_or_outside:
            end = k
            outside_count = 0
            env_high = max(env_high, h)
            env_low = min(env_low, l)
        else:
            outside_count += 1
            if outside_count >= 2:
                break
    bars = end - start + 1
    if bars < min_bars:
        return None
    part = ctx.iloc[start:end + 1]
    adjacent_overlap_count = int(part["overlaps_prev_bar"].iloc[1:].sum())
    adjacent_pairs = max(1, bars - 1)
    return {
        "start": part.index[0],
        "end": part.index[-1],
        "bars": bars,
        "high": float(part["high"].max()),
        "low": float(part["low"].min()),
        "range_height": float(part["high"].max() - part["low"].min()),
        "adjacent_overlap_count": adjacent_overlap_count,
        "adjacent_pairs": adjacent_pairs,
        "adjacent_overlap_ratio": adjacent_overlap_count / adjacent_pairs,
        "anchor_context": str(ctx["bar_context"].iloc[start]),
    }


def detect_basic_overlap_clusters(ctx: pd.DataFrame, min_bars: int = 3, lookahead: int = 12) -> pd.DataFrame:
    anchors = set()
    for i, (_, r) in enumerate(ctx.iterrows()):
        if bool(r["doji_bar"]) or bool(r["outside_bar"]) or bool(r["bull_reversal_bar"]) or bool(r["bear_reversal_bar"]):
            anchors.add(i)
    runs = detect_consecutive_runs(ctx, min_run=3)
    if not runs.empty:
        index_map = {t: i for i, t in enumerate(ctx.index)}
        for _, r in runs.iterrows():
            next_bar = r.get("next_bar")
            if pd.notna(next_bar) and next_bar in index_map:
                anchors.add(index_map[next_bar])
            if r["end"] in index_map:
                anchors.add(index_map[r["end"]])
    clusters = []
    for i in sorted(anchors):
        c = local_overlap_cluster_from_anchor(ctx, anchor_idx=i, lookahead=lookahead, min_bars=min_bars)
        if c is not None:
            clusters.append(c)
    if not clusters:
        return pd.DataFrame()
    return pd.DataFrame(clusters).drop_duplicates(subset=["start", "end"]).sort_values(["start", "end"]).reset_index(drop=True)


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
    doji_body_ratio = 0.25
    strong_body_ratio = 0.55
    close_near_extreme = 0.70
    tail_ratio = 0.35
    breakout_lookback = 1
    min_run = 3
    doji_breaks_run = True
    cluster_min_bars = 3
    cluster_lookahead = 12
    out_prefix = "detect_ttr_basic"
    show_bars = False

    df = load_ohlc_file(
        ohlc_path,
        datetime_col=datetime_col,
        message_col=message_col,
        source_tz_if_naive=source_tz_if_naive,
        target_tz=target_tz,
        round_to_minute=round_to_minute,
    )

    try:
        df = slice_ohlc(df, start_datetime, end_datetime)
    except ValueError as e:
        print(e)
        return

    df = resample_ohlc(df, resample_rule)
    ctx = add_bar_context(
        df,
        doji_body_ratio=doji_body_ratio,
        strong_body_ratio=strong_body_ratio,
        close_near_extreme=close_near_extreme,
        tail_ratio=tail_ratio,
        breakout_lookback=breakout_lookback,
    )
    runs = detect_consecutive_runs(ctx, min_run=min_run, include_doji_as_break=doji_breaks_run)
    clusters = detect_basic_overlap_clusters(ctx, min_bars=cluster_min_bars, lookahead=cluster_lookahead)

    prefix = Path(out_prefix)
    bar_path = prefix.with_name(prefix.name + "_bar_context.csv")
    run_path = prefix.with_name(prefix.name + "_consecutive_runs.csv")
    cluster_path = prefix.with_name(prefix.name + "_overlap_clusters.csv")
    ctx.to_csv(bar_path, encoding="utf-8-sig")
    runs.to_csv(run_path, encoding="utf-8-sig", index=False)
    clusters.to_csv(cluster_path, encoding="utf-8-sig", index=False)

    print(f"version={VERSION}")
    print(f"bars_loaded={len(ctx)} start={ctx.index[0]} end={ctx.index[-1]} resample={resample_rule}")
    print(f"saved_bar_context={bar_path}")
    print(f"saved_consecutive_runs={run_path}")
    print(f"saved_overlap_clusters={cluster_path}")
    print("\nConsecutive runs:")
    print("(none)" if runs.empty else runs.to_string(index=False))
    print("\nBasic overlap clusters:")
    print("(none)" if clusters.empty else clusters.to_string(index=False))
    if show_bars:
        cols = [
            "open", "high", "low", "close", "bar_direction", "body_ratio",
            "upper_tail_ratio", "lower_tail_ratio", "inside_bar", "outside_bar",
            "buy_trigger_prev_high", "sell_trigger_prev_low", "bull_reversal_bar",
            "bear_reversal_bar", "bull_breakout_bar", "bear_breakout_bar", "bar_context",
        ]
        print("\nBar context:")
        print(ctx[cols].to_string())


if __name__ == "__main__":
    main()
