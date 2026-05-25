import os
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Optional

OHLC_PATH = r"C:\Users\weivo\OneDrive\เอกสาร\Price Action Trading\david\ohlc"

def normalize_ohlc_columns(df: pd.DataFrame, message_col: str = "Message") -> pd.DataFrame:
    """Return dataframe with lowercase columns: open, high, low, close."""
    df = df.copy()

    if message_col in df.columns:
        ohlc = df[message_col].astype(str).str.split(",", expand=True)
        if ohlc.shape[1] < 4:
            raise ValueError(f"Column '{message_col}' must contain open,high,low,close")
        df[["open", "high", "low", "close"]] = ohlc.iloc[:, :4].astype(float)
        return df[["open", "high", "low", "close"]]

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
    """Set DatetimeIndex, convert to target_tz, then remove timezone."""
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


def slice_ohlc(
    df: pd.DataFrame,
    start: Optional[str],
    end: Optional[str],
    window_size: int = 0,
) -> pd.DataFrame:
    """Slice OHLC dataframe by either datetime range or fixed bar window."""
    if window_size < 0:
        raise ValueError("window_size must be >= 0")

    if window_size > 0:
        if start:
            anchor = pd.to_datetime(start)
            candidates = df.loc[df.index >= anchor].copy()
        else:
            candidates = df.copy()

        out = candidates.iloc[:window_size].copy()
        if out.empty:
            raise ValueError(f"No OHLC rows found from start={start} for window_size={window_size}")
        if len(out) < window_size:
            raise ValueError(
                f"Only {len(out)} bar(s) available from start={start}, "
                f"but window_size={window_size} was requested."
            )
        return out

    if start or end:
        df = df.loc[start:end].copy()
    if df.empty:
        raise ValueError(f"No OHLC rows found between start={start} and end={end}")
    return df


def close_location_value(row) -> float:
    """
    0 = close at low
    1 = close at high
    """
    rng = row["high"] - row["low"]
    if rng <= 0:
        return 0.5
    return (row["close"] - row["low"]) / rng


def body_ratio(row) -> float:
    """
    Body size / bar range.
    """
    rng = row["high"] - row["low"]
    if rng <= 0:
        return 0.0
    return abs(row["close"] - row["open"]) / rng


def overlap_ratio(df: pd.DataFrame) -> float:
    """
    Measures how much adjacent bars overlap.
    Higher value = more trading-range-like.
    """
    if len(df) < 2:
        return 0.0

    overlaps = []

    for i in range(1, len(df)):
        prev = df.iloc[i - 1]
        cur = df.iloc[i]

        overlap_high = min(prev["high"], cur["high"])
        overlap_low = max(prev["low"], cur["low"])
        overlap = max(0.0, overlap_high - overlap_low)

        cur_range = cur["high"] - cur["low"]
        if cur_range > 0:
            overlaps.append(overlap / cur_range)

    return float(np.mean(overlaps)) if overlaps else 0.0


def directional_efficiency(df: pd.DataFrame) -> float:
    """
    Net movement / total path.
    Low value = sideways / range.
    High value = directional trend.
    """
    if len(df) < 2:
        return 0.0

    net = abs(df["close"].iloc[-1] - df["close"].iloc[0])
    path = df["close"].diff().abs().sum()

    if path <= 0:
        return 0.0

    return float(net / path)


def detect_range_context(
    df: pd.DataFrame,
    lookback: int = 80,
    exclude_recent: int = 5,
    min_overlap: float = 0.45,
    max_efficiency: float = 0.35,
    min_range_units: float = 8.0,
) -> dict:
    """
    Detect whether the prior context behaves like a trading range.

    exclude_recent:
        Exclude most recent bars so a current bull spike does not distort
        the range boundary too much.
    """
    if len(df) < lookback + exclude_recent:
        return {
            "is_trading_range": False,
            "reason": "not_enough_bars",
        }

    context = df.iloc[-lookback - exclude_recent : -exclude_recent].copy()

    range_high = float(context["high"].max())
    range_low = float(context["low"].min())
    range_height = range_high - range_low

    bar_ranges = context["high"] - context["low"]
    typical_bar_range = float(bar_ranges.median())

    if typical_bar_range <= 0:
        typical_bar_range = float(bar_ranges.mean())

    ov = overlap_ratio(context)
    eff = directional_efficiency(context)

    range_units = range_height / typical_bar_range if typical_bar_range > 0 else 0.0

    # Broad trading range can have large height,
    # but still low directional efficiency and high overlap.
    is_tr = (
        ov >= min_overlap
        and eff <= max_efficiency
        and range_units >= min_range_units
    )

    return {
        "is_trading_range": bool(is_tr),
        "range_high": range_high,
        "range_low": range_low,
        "range_height": range_height,
        "typical_bar_range": typical_bar_range,
        "overlap_ratio": ov,
        "directional_efficiency": eff,
        "range_units": range_units,
    }


def detect_bull_spike(
    df: pd.DataFrame,
    spike_bars: int = 5,
    min_bull_ratio: float = 0.65,
    min_close_location: float = 0.65,
    min_net_move_units: float = 3.0,
    typical_bar_range: float | None = None,
) -> dict:
    """
    Detect whether the most recent bars form a bull spike.
    """
    if len(df) < spike_bars:
        return {
            "is_bull_spike": False,
            "reason": "not_enough_bars",
        }

    recent = df.iloc[-spike_bars:].copy()

    bull_bars = recent["close"] > recent["open"]
    bull_ratio = float(bull_bars.mean())

    clv = recent.apply(close_location_value, axis=1)
    avg_clv = float(clv.mean())

    bodies = recent.apply(body_ratio, axis=1)
    avg_body = float(bodies.mean())

    net_move = float(recent["close"].iloc[-1] - recent["open"].iloc[0])

    if typical_bar_range is None:
        bar_ranges = df["high"] - df["low"]
        typical_bar_range = float(bar_ranges.iloc[-30:].median())

    net_move_units = net_move / typical_bar_range if typical_bar_range and typical_bar_range > 0 else 0.0

    is_spike = (
        bull_ratio >= min_bull_ratio
        and avg_clv >= min_close_location
        and net_move_units >= min_net_move_units
        and net_move > 0
    )

    return {
        "is_bull_spike": bool(is_spike),
        "bull_ratio": bull_ratio,
        "avg_close_location": avg_clv,
        "avg_body_ratio": avg_body,
        "net_move": net_move,
        "net_move_units": net_move_units,
        "spike_start": recent.index[0],
        "spike_end": recent.index[-1],
    }


def classify_bull_spike_context(
    df: pd.DataFrame,
    lookback: int = 80,
    spike_bars: int = 5,
    top_zone: float = 0.80,
    breakout_buffer_units: float = 0.5,
    followthrough_bars: int = 3,
    range_ctx: dict | None = None,
) -> dict:
    """
    Context-aware interpretation of a bull spike.

    Key idea:
    A bull spike is not automatically a new bull trend.
    It depends on where it occurs relative to the prior range.

    This function does NOT claim to detect every price action behavior.
    It only separates:
    - bull spike inside / near high of trading range
    - bull breakout attempt
    - possible bull trend continuation
    """

    df = normalize_ohlc_columns(df)

    if range_ctx is None:
        range_ctx = detect_range_context(
            df,
            lookback=lookback,
            exclude_recent=spike_bars,
        )

    typical = range_ctx.get("typical_bar_range", None)

    spike = detect_bull_spike(
        df,
        spike_bars=spike_bars,
        typical_bar_range=typical,
    )

    if not spike["is_bull_spike"]:
        return {
            "classification": "no_clear_bull_spike",
            "strategy_bias": None,
            "message": "No clear bull spike was detected in the selected time range.",
            "range_context": range_ctx,
            "bull_spike": spike,
        }

    latest_close = float(df["close"].iloc[-1])
    latest_high = float(df["high"].iloc[-1])

    if not range_ctx["is_trading_range"]:
        return {
            "classification": "bull_spike_without_confirmed_trading_range_context",
            "strategy_bias": "context_unclear_wait_for_confirmation",
            "message": (
                "There is a bull spike, but the prior context is not clearly "
                "classified as a trading range. It may be trend behavior or "
                "part of another structure."
            ),
            "range_context": range_ctx,
            "bull_spike": spike,
        }

    range_high = range_ctx["range_high"]
    range_low = range_ctx["range_low"]
    range_height = range_ctx["range_height"]
    typical_bar_range = range_ctx["typical_bar_range"]

    close_position = (
        (latest_close - range_low) / range_height
        if range_height > 0
        else 0.5
    )

    breakout_buffer = breakout_buffer_units * typical_bar_range
    breakout_level = range_high + breakout_buffer

    closes_above_range = df["close"].iloc[-followthrough_bars:] > range_high
    highs_above_range = df["high"].iloc[-followthrough_bars:] > range_high

    breakout_attempt = latest_high > range_high
    confirmed_breakout = latest_close > breakout_level
    has_followthrough = bool(closes_above_range.mean() >= 0.67)

    # Case 1:
    # Bull spike into upper part of established trading range,
    # but no confirmed breakout.
    if close_position >= top_zone and not confirmed_breakout:
        return {
            "classification": "bull_spike_at_trading_range_high_exhaustion_risk",
            "strategy_bias": "avoid_chasing_long_consider_short_setup_if_reversal_signal_appears",
            "message": (
                "Bull spike is strong, but it occurs near the high of an established "
                "trading range and has not confirmed a breakout. This is not automatically "
                "a start of a new bull trend. It may be exhaustive buying at the range high."
            ),
            "close_position_in_range": close_position,
            "breakout_attempt": breakout_attempt,
            "confirmed_breakout": confirmed_breakout,
            "has_followthrough": has_followthrough,
            "range_context": range_ctx,
            "bull_spike": spike,
        }

    # Case 2:
    # Bull spike breaks above range but follow-through is not known/weak.
    if breakout_attempt and confirmed_breakout and not has_followthrough:
        return {
            "classification": "bull_breakout_attempt_pending_followthrough",
            "strategy_bias": "do_not_assume_new_trend_until_followthrough",
            "message": (
                "Bull spike broke above the trading range, but follow-through is not strong "
                "enough yet. This can still fail and become a failed breakout."
            ),
            "close_position_in_range": close_position,
            "breakout_attempt": breakout_attempt,
            "confirmed_breakout": confirmed_breakout,
            "has_followthrough": has_followthrough,
            "range_context": range_ctx,
            "bull_spike": spike,
        }

    # Case 3:
    # Bull breakout with follow-through.
    if breakout_attempt and confirmed_breakout and has_followthrough:
        return {
            "classification": "possible_new_bull_trend_after_range_breakout",
            "strategy_bias": "bull_breakout_has_followthrough",
            "message": (
                "Bull spike broke above the prior trading range and has follow-through. "
                "This has more evidence for a possible new bull trend."
            ),
            "close_position_in_range": close_position,
            "breakout_attempt": breakout_attempt,
            "confirmed_breakout": confirmed_breakout,
            "has_followthrough": has_followthrough,
            "range_context": range_ctx,
            "bull_spike": spike,
        }

    # Case 4:
    # Bull spike inside the middle/lower part of range.
    return {
        "classification": "bull_spike_inside_trading_range",
        "strategy_bias": "context_dependent_take_profit_near_range_high",
        "message": (
            "Bull spike occurred inside a trading range. It may be tradable, but "
            "the high of the range is resistance unless there is a confirmed breakout."
        ),
        "close_position_in_range": close_position,
        "breakout_attempt": breakout_attempt,
        "confirmed_breakout": confirmed_breakout,
        "has_followthrough": has_followthrough,
        "range_context": range_ctx,
        "bull_spike": spike,
    }


def main() -> None:
    ohlc_path = ""

    while True:
        try:
            symbol = input("Symbol: ").strip()
            exchange = input("Exchange: ").strip()
            timeframe = input("Timeframe: ").strip()
            start_datetime = input("Start datetime: ").strip()
            end_datetime = input("End datetime: ").strip()

            instrument = symbol + "-" + exchange + "-" + timeframe + ".csv"
            ohlc_path = os.path.join(OHLC_PATH, instrument)

            if os.path.exists(ohlc_path):
                break

            print(f"File not found: {ohlc_path}")
        except Exception as e:
            print(f"Error: {e}")

    df = load_ohlc_file(
        ohlc_path,
        message_col="Message",
        source_tz_if_naive="UTC",
        target_tz="Asia/Bangkok",
    )

    try:
        df_selected = slice_ohlc(
            df,
            start=start_datetime,
            end=end_datetime,
        )
    except ValueError as e:
        print(e)
        return

    range_context = detect_range_context(
        df_selected,
        lookback=80,
        exclude_recent=5,
    )

    result = classify_bull_spike_context(
        df_selected,
        lookback=80,
        spike_bars=5,
        top_zone=0.80,
        breakout_buffer_units=0.5,
        followthrough_bars=3,
        range_ctx=range_context,
    )

    classification = result["classification"]
    strategy_bias = result["strategy_bias"]
    message = result["message"]

    print("Range context:")
    print(range_context)
    print(classification)
    print(strategy_bias)
    print(message)


if __name__ == "__main__":
    main()
