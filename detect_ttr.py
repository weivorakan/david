"""
detect_ttr.py

Back-to-basic Bar Context Builder.

Purpose of this version:
1) Load an OHLC file from the ./ohlc folder.
2) Create a concise CSV containing OHLC + summarized bar context.
3) Detect consecutive breakout/trend bar runs in the same direction.

Important:
- This version intentionally DOES NOT detect overlapped bars or TTR yet.
- The goal is to build reliable bar-by-bar context first.
- Definitions are based on "Candlestick Bar Characteristics.txt" and the
  user's simplified midpoint/prior-bar interpretation.

Interactive example:
    .\\venv\\Scripts\\python.exe .\\detect_ttr.py

Command-line example:
    .\\venv\\Scripts\\python.exe .\\detect_ttr.py --input "XAUUSD-GO Markets-1m.csv" ^
      --start "2026-05-13 19:00" --end "2026-05-13 22:40"
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd


VERSION = "2026-05-23-bar-context-v5"


# -----------------------------
# Rules
# -----------------------------


@dataclass(frozen=True)
class BarContextRules:
    """
    Simple, visible rules for this early step.

    No official Al Brooks numeric formula is assumed here. These thresholds are
    practical starting points so the output can be inspected and adjusted.
    """

    # User-friendly first approximation:
    # body must be less than/equal to half of the whole bar before a bar can be
    # considered doji/trading-range-like, but body size alone is not enough.
    doji_body_max_ratio: float = 0.50

    # Tail classification is still experimental. These first thresholds are
    # based on tail_size / bar_size because the user's visual examples separate
    # well around these levels:
    #   0.00      = no tail
    #   <= 0.10   = almost no tail
    #   <= 0.30   = small tail
    #   > 0.30    = long/prominent tail
    almost_no_tail_max_ratio: float = 0.10
    small_tail_max_ratio: float = 0.30

    # Minimum number of consecutive breakout/trend bars to report as a group.
    min_consecutive_bars: int = 3

    # Keep a compact high/low comparison against prior 1..N bars for later
    # overlap/TTR experiments.
    lookback_bars: int = 5


# -----------------------------
# Data loading / normalization
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

    if pd.isna(dt).any():
        raise ValueError(f"Datetime parsing failed for {int(pd.isna(dt).sum())} row(s).")

    idx = pd.DatetimeIndex(dt)
    if idx.tz is None:
        idx = idx.tz_localize(source_tz_if_naive).tz_convert(target_tz)
    else:
        idx = idx.tz_convert(target_tz)

    # Keep output and slicing in local Bangkok time.
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
    suffix = input_path.suffix.lower()

    if suffix == ".csv":
        raw = pd.read_csv(input_path, index_col=0 if datetime_col is None else None)
    elif suffix in (".xlsx", ".xls"):
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
        df = df.loc[start or None : end or None].copy()
    if df.empty:
        raise ValueError(f"No OHLC rows found between start={start!r} and end={end!r}")
    return df


# -----------------------------
# Bar context
# -----------------------------


def compare_high_low(current_high: float, current_low: float, prior_high: float, prior_low: float) -> str:
    """Compact high/low relation against a prior bar's high and low."""
    if pd.isna(prior_high) or pd.isna(prior_low):
        return "no_prior_bar"

    if current_high < prior_high and current_low > prior_low:
        return "inside_bar"
    if current_high > prior_high and current_low < prior_low:
        return "outside_bar"
    if current_high > prior_high and current_low > prior_low:
        return "higher_high_higher_low"
    if current_high < prior_high and current_low < prior_low:
        return "lower_low_lower_high"
    if current_high > prior_high:
        return "higher_high_only"
    if current_low < prior_low:
        return "lower_low_only"
    return "same_or_equal_high_low"


def compare_close_to_prior(
    close: float,
    prior_high: float,
    prior_low: float,
    is_bull: bool,
    is_bear: bool,
) -> str:
    """
    Directional close test against the prior bar.

    - Bull bar: the important question is whether it closes above/below prior high.
    - Bear bar: the important question is whether it closes below/above prior low.
    """
    if pd.isna(prior_high) or pd.isna(prior_low):
        return "no_prior_bar"

    if is_bull:
        if close > prior_high:
            return "close_above_prior_high"
        if close < prior_high:
            return "close_below_prior_high"
        return "close_at_prior_high"

    if is_bear:
        if close < prior_low:
            return "close_below_prior_low"
        if close > prior_low:
            return "close_above_prior_low"
        return "close_at_prior_low"

    return "neutral_bar_no_directional_prior_close_test"


def classify_bar_context(
    is_bull: bool,
    is_bear: bool,
    is_doji: bool,
    close: float,
    midpoint: float,
    closes_beyond_prior_breakout_level: bool,
) -> str:
    """
    Single-column bar meaning.

    Important current simplification:
    - Breakout bar and doji bar are mutually exclusive.
    - Reversal bar and doji bar can coexist.
    """
    if not is_bull and not is_bear:
        return "Neutral doji" if is_doji else "Neutral bar"

    if is_bull:
        if close > midpoint and closes_beyond_prior_breakout_level:
            return "Bull breakout bar"
        if close <= midpoint:
            return "Bull reversal bar, Bull doji" if is_doji else "Bull reversal bar"
        if is_doji:
            return "Bull doji"
        return "Bull breakout bar"

    if is_bear:
        if close < midpoint and closes_beyond_prior_breakout_level:
            return "Bear breakout bar"
        if close >= midpoint:
            return "Bear reversal bar, Bear doji" if is_doji else "Bear reversal bar"
        if is_doji:
            return "Bear doji"
        return "Bear breakout bar"

    return "Unknown bar"


def classify_one_tail(tail_ratio: float, side: str, rules: BarContextRules) -> str:
    if tail_ratio <= 0:
        return f"no_{side}_tail"
    if tail_ratio <= rules.almost_no_tail_max_ratio:
        return f"almost_no_{side}_tail"
    if tail_ratio <= rules.small_tail_max_ratio:
        return f"small_{side}_tail"
    return f"long_{side}_tail"


def build_lookback_context(out: pd.DataFrame, row_position: int, lookback_bars: int) -> str:
    """
    Build sequence context:
    1 = current bar compared with its prior bar
    2 = prior bar compared with its own prior bar
    3 = two bars ago compared with its own prior bar
    ...
    """
    pieces = []
    for k in range(1, lookback_bars + 1):
        current_position = row_position - (k - 1)
        prior_position = row_position - k
        if prior_position < 0:
            break
        row = out.iloc[current_position]
        prior = out.iloc[prior_position]
        relation = compare_high_low(row["high"], row["low"], prior["high"], prior["low"])
        pieces.append(f"{k}:{relation}")
    return ";".join(pieces) if pieces else "no_prior_bar"


def add_bar_context(df: pd.DataFrame, rules: BarContextRules) -> pd.DataFrame:
    out = df.copy()

    out["bar_size"] = out["high"] - out["low"]
    safe_size = out["bar_size"].where(out["bar_size"] != 0)
    out["body_size"] = (out["close"] - out["open"]).abs()
    out["body_ratio"] = (out["body_size"] / safe_size).fillna(0.0)
    out["upper_tail"] = (out["high"] - out[["open", "close"]].max(axis=1)).clip(lower=0)
    out["lower_tail"] = (out[["open", "close"]].min(axis=1) - out["low"]).clip(lower=0)
    out["upper_tail_ratio"] = (out["upper_tail"] / safe_size).fillna(0.0)
    out["lower_tail_ratio"] = (out["lower_tail"] / safe_size).fillna(0.0)
    out["mid_price"] = out["low"] + (out["bar_size"] / 2.0)

    out["_is_bull"] = out["close"] > out["open"]
    out["_is_bear"] = out["close"] < out["open"]

    # Doji should represent two-sided trading, not merely a body <= 50%.
    # 1) Normal doji: small body + meaningful tails on both sides.
    # 2) Reversal-like doji: small body + close on reversal side of midpoint
    #    + prominent reversal tail.
    small_body = out["body_ratio"] <= rules.doji_body_max_ratio
    meaningful_upper_tail = out["upper_tail_ratio"] > rules.almost_no_tail_max_ratio
    meaningful_lower_tail = out["lower_tail_ratio"] > rules.almost_no_tail_max_ratio
    two_sided_tails = meaningful_upper_tail & meaningful_lower_tail

    bull_reversal_side = out["_is_bull"] & (out["close"] <= out["mid_price"])
    bear_reversal_side = out["_is_bear"] & (out["close"] >= out["mid_price"])
    prominent_bull_reversal_tail = out["upper_tail_ratio"] > rules.small_tail_max_ratio
    prominent_bear_reversal_tail = out["lower_tail_ratio"] > rules.small_tail_max_ratio
    reversal_like_tail = (
        (bull_reversal_side & prominent_bull_reversal_tail)
        | (bear_reversal_side & prominent_bear_reversal_tail)
    )

    out["_is_doji"] = small_body & (two_sided_tails | reversal_like_tail)

    out["upper_tail_context"] = [
        classify_one_tail(ratio, "upper", rules) for ratio in out["upper_tail_ratio"]
    ]
    out["lower_tail_context"] = [
        classify_one_tail(ratio, "lower", rules) for ratio in out["lower_tail_ratio"]
    ]
    out["tail_context"] = out["upper_tail_context"] + ", " + out["lower_tail_context"]

    out["close_vs_midpoint"] = [
        "close_above_midpoint" if close > mid else "close_below_midpoint" if close < mid else "close_at_midpoint"
        for close, mid in zip(out["close"], out["mid_price"])
    ]

    prev_high = out["high"].shift(1)
    prev_low = out["low"].shift(1)
    out["breakout_depth_vs_prior"] = [
        close - ph if is_bull and not pd.isna(ph) and close > ph
        else pl - close if is_bear and not pd.isna(pl) and close < pl
        else 0.0
        for close, ph, pl, is_bull, is_bear in zip(
            out["close"], prev_high, prev_low, out["_is_bull"], out["_is_bear"]
        )
    ]
    closes_beyond_prior_breakout_level = [depth > 0 for depth in out["breakout_depth_vs_prior"]]

    out["bar_context"] = [
        classify_bar_context(is_bull, is_bear, is_doji, close, midpoint, beyond_prior)
        for is_bull, is_bear, is_doji, close, midpoint, beyond_prior in zip(
            out["_is_bull"],
            out["_is_bear"],
            out["_is_doji"],
            out["close"],
            out["mid_price"],
            closes_beyond_prior_breakout_level,
        )
    ]
    out["prior_bar_context"] = [
        compare_high_low(h, l, ph, pl)
        for h, l, ph, pl in zip(out["high"], out["low"], prev_high, prev_low)
    ]
    out["bar_context"] = [
        f"{bar_context}, {prior_context}"
        if prior_context in ("inside_bar", "outside_bar")
        else bar_context
        for bar_context, prior_context in zip(out["bar_context"], out["prior_bar_context"])
    ]
    out["close_vs_prior_high_low"] = [
        compare_close_to_prior(c, ph, pl, is_bull, is_bear)
        for c, ph, pl, is_bull, is_bear in zip(
            out["close"], prev_high, prev_low, out["_is_bull"], out["_is_bear"]
        )
    ]

    out["lookback_1_to_5"] = [
        build_lookback_context(out, row_position=i, lookback_bars=rules.lookback_bars)
        for i in range(len(out))
    ]

    return out


# -----------------------------
# Consecutive bars
# -----------------------------


def trend_direction_for_consecutive(row: pd.Series) -> Optional[str]:
    if str(row["bar_context"]).startswith("Bull breakout bar"):
        return "bull"
    if str(row["bar_context"]).startswith("Bear breakout bar"):
        return "bear"
    return None


def keeps_consecutive_structure(prev: pd.Series, cur: pd.Series, direction: str) -> bool:
    if direction == "bull":
        return (cur["high"] > prev["high"]) and (cur["low"] > prev["low"])
    if direction == "bear":
        return (cur["low"] < prev["low"]) and (cur["high"] < prev["high"])
    return False


def detect_consecutive_runs(ctx: pd.DataFrame, min_bars: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    ctx = ctx.copy()
    ctx["consecutive_direction"] = ""
    ctx["consecutive_run_id"] = ""
    ctx["consecutive_position"] = ""
    ctx["consecutive_run_length"] = 0

    runs: list[dict] = []
    n = len(ctx)
    i = 0

    while i < n:
        direction = trend_direction_for_consecutive(ctx.iloc[i])
        if direction is None:
            i += 1
            continue

        start_i = i
        end_i = i
        j = i + 1

        while j < n:
            cur_direction = trend_direction_for_consecutive(ctx.iloc[j])
            if cur_direction != direction:
                break
            if not keeps_consecutive_structure(ctx.iloc[j - 1], ctx.iloc[j], direction):
                break
            end_i = j
            j += 1

        run_len = end_i - start_i + 1
        if run_len >= min_bars:
            part = ctx.iloc[start_i : end_i + 1]
            run_id = f"{direction}_{len(runs) + 1:03d}"
            runs.append(
                {
                    "run_id": run_id,
                    "direction": direction,
                    "start_datetime": part.index[0],
                    "end_datetime": part.index[-1],
                    "bars": int(run_len),
                    "start_open": float(part["open"].iloc[0]),
                    "end_close": float(part["close"].iloc[-1]),
                    "range_high": float(part["high"].max()),
                    "range_low": float(part["low"].min()),
                    "bar_contexts": " -> ".join(part["bar_context"].astype(str).tolist()),
                }
            )

            for position, row_i in enumerate(range(start_i, end_i + 1), start=1):
                ctx.iat[row_i, ctx.columns.get_loc("consecutive_direction")] = direction
                ctx.iat[row_i, ctx.columns.get_loc("consecutive_run_id")] = run_id
                ctx.iat[row_i, ctx.columns.get_loc("consecutive_position")] = f"{position}/{run_len}"
                ctx.iat[row_i, ctx.columns.get_loc("consecutive_run_length")] = run_len

        i = end_i + 1 if end_i > start_i else start_i + 1

    runs_df = pd.DataFrame(
        runs,
        columns=[
            "run_id",
            "direction",
            "start_datetime",
            "end_datetime",
            "bars",
            "start_open",
            "end_close",
            "range_high",
            "range_low",
            "bar_contexts",
        ],
    )

    return ctx, runs_df


# -----------------------------
# File paths / output
# -----------------------------


def resolve_input_path(input_value: str, ohlc_dir: Path) -> Path:
    p = Path(input_value)
    if p.exists():
        return p

    p2 = ohlc_dir / input_value
    if p2.exists():
        return p2

    raise FileNotFoundError(f"File not found: {p} or {p2}")


def safe_filename_part(value: Optional[str]) -> str:
    if not value:
        return "all"
    value = value.strip()
    value = re.sub(r"[^\w\-]+", "_", value, flags=re.UNICODE)
    return value.strip("_") or "all"


def make_output_paths(input_path: Path, start: Optional[str], end: Optional[str], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{safe_filename_part(start)}_to_{safe_filename_part(end)}"
    stem = input_path.stem
    context_path = output_dir / f"{stem}_bar_context_{stamp}.csv"
    runs_path = output_dir / f"{stem}_consecutive_bars_{stamp}.csv"
    return context_path, runs_path


def dataframe_for_csv(ctx: pd.DataFrame) -> pd.DataFrame:
    out = ctx.copy()
    out.insert(0, "datetime", format_datetime_minute(out.index))

    # Keep the output readable. Internal boolean columns are intentionally not exported.
    columns = [
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "bar_size",
        "mid_price",
        "body_size",
        "body_ratio",
        "bar_context",
        "upper_tail",
        "lower_tail",
        "upper_tail_ratio",
        "lower_tail_ratio",
        "tail_context",
        "close_vs_prior_high_low",
        "breakout_depth_vs_prior",
        "lookback_1_to_5",
    ]
    return out[columns]


def format_datetime_minute(values) -> list[str]:
    return pd.DatetimeIndex(values).strftime("%Y-%m-%d %H:%M").tolist()


def format_runs_for_csv(runs: pd.DataFrame) -> pd.DataFrame:
    out = runs.copy()
    if not out.empty:
        out["start_datetime"] = format_datetime_minute(out["start_datetime"])
        out["end_datetime"] = format_datetime_minute(out["end_datetime"])
    return out


# -----------------------------
# CLI / interactive
# -----------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build concise candlestick bar context and detect consecutive bars.")
    parser.add_argument("--input", help="OHLC file path or file name inside ./ohlc")
    parser.add_argument("--start", default="", help='Start datetime, e.g. "2026-05-13 19:00"')
    parser.add_argument("--end", default="", help='End datetime, e.g. "2026-05-13 22:40"')
    parser.add_argument("--datetime-col", default=None, help="Datetime column name if datetime is not the index")
    parser.add_argument("--message-col", default="Message", help='Column containing "open,high,low,close"')
    parser.add_argument("--source-tz-if-naive", default="UTC")
    parser.add_argument("--target-tz", default="Asia/Bangkok")
    parser.add_argument("--min-consecutive-bars", type=int, default=BarContextRules.min_consecutive_bars)
    parser.add_argument("--output-dir", default="bar_context_output")
    return parser


def prompt_for_input_file(ohlc_dir: Path) -> Path:
    while True:
        symbol = input("Symbol: ").strip()
        exchange = input("Exchange: ").strip()
        timeframe = input("Timeframe: ").strip()
        instrument = f"{symbol}-{exchange}-{timeframe}.csv"
        candidate = ohlc_dir / instrument

        if candidate.exists():
            return candidate

        print(f"File not found: {candidate}")
        print('Example: Symbol=XAUUSD, Exchange=GO Markets, Timeframe=1m')


def main() -> None:
    args = build_arg_parser().parse_args()
    project_dir = Path(__file__).resolve().parent
    ohlc_dir = project_dir / "ohlc"

    if args.input:
        input_path = resolve_input_path(args.input, ohlc_dir)
        start_datetime = args.start.strip()
        end_datetime = args.end.strip()
    else:
        input_path = prompt_for_input_file(ohlc_dir)
        start_datetime = input("Start datetime (blank = first bar): ").strip()
        end_datetime = input("End datetime (blank = last bar): ").strip()

    rules = BarContextRules(min_consecutive_bars=args.min_consecutive_bars)

    df = load_ohlc_file(
        input_path,
        datetime_col=args.datetime_col,
        message_col=args.message_col,
        source_tz_if_naive=args.source_tz_if_naive,
        target_tz=args.target_tz,
        round_to_minute=False,
    )
    df = slice_ohlc(df, start_datetime, end_datetime)

    ctx = add_bar_context(df, rules)
    ctx, runs = detect_consecutive_runs(ctx, min_bars=rules.min_consecutive_bars)

    context_path, runs_path = make_output_paths(
        input_path=input_path,
        start=start_datetime,
        end=end_datetime,
        output_dir=project_dir / args.output_dir,
    )

    dataframe_for_csv(ctx).to_csv(context_path, index=False, encoding="utf-8-sig")
    formatted_runs = format_runs_for_csv(runs)
    formatted_runs.to_csv(runs_path, index=False, encoding="utf-8-sig")

    print(f"version={VERSION}")
    print(f"input={input_path}")
    start_text, end_text = format_datetime_minute([ctx.index[0], ctx.index[-1]])
    print(f"bars_loaded={len(ctx)} start={start_text} end={end_text}")
    print(f"saved_bar_context={context_path}")
    print(f"saved_consecutive_bars={runs_path}")
    print(f"consecutive_runs={len(runs)} min_bars={rules.min_consecutive_bars}")

    if not formatted_runs.empty:
        print("\nConsecutive bars:")
        display_cols = ["run_id", "direction", "start_datetime", "end_datetime", "bars"]
        print(formatted_runs[display_cols].to_string(index=False))
    else:
        print("\nNo consecutive breakout/trend bar runs found with the current basic rules.")


if __name__ == "__main__":
    main()
