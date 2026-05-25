"""
ohlc_image_rendering.py

Standalone OHLC image renderer for David project.
- Reads CSV or Excel OHLC files.
- Supports Wei's current format: Datetime as index/first column + Message = "open,high,low,close".
- Handles timezone-aware Datetime and converts to Asia/Bangkok, then removes timezone.
- Renders candlestick chart to PNG/JPG using mplfinance if installed, otherwise pure matplotlib.

Install optional packages:
    pip install pandas matplotlib openpyxl mplfinance

Example:
    python ohlc_image_rendering.py \
        --input "C:\\david\\ohlc\\XAUUSD-Go Markets-1m.csv" \
        --output "C:\\david\\test\\xauusd_render.png" \
        --start "2026-04-24 20:00" \
        --end "2026-04-24 21:00" \
        --title "XAUUSD 1m"
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Optional

import pandas as pd


def normalize_ohlc_columns(df: pd.DataFrame, message_col: str = "Message") -> pd.DataFrame:
    """Return dataframe with columns: open, high, low, close.

    Supported formats:
    1) Message column contains "O,H,L,C" string.
    2) Separate OHLC columns with names like Open/High/Low/Close or open/high/low/close.
    """
    df = df.copy()

    if message_col in df.columns:
        ohlc = df[message_col].astype(str).str.split(",", expand=True)
        if ohlc.shape[1] < 4:
            raise ValueError(f"Column '{message_col}' must contain 4 comma-separated values: open,high,low,close")
        df[["open", "high", "low", "close"]] = ohlc.iloc[:, :4].astype(float)
        df = df.drop(columns=[message_col])
        return df[["open", "high", "low", "close"]]

    lower_map = {str(c).strip().lower(): c for c in df.columns}
    required = ["open", "high", "low", "close"]
    missing = [c for c in required if c not in lower_map]
    if missing:
        raise ValueError(
            "Could not find OHLC data. Expected either Message='open,high,low,close' "
            f"or separate columns open/high/low/close. Missing: {missing}"
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
    """Set/clean DatetimeIndex.

    If datetime has timezone, convert to target_tz.
    If datetime is naive, assume source_tz_if_naive, then convert to target_tz.
    Finally remove timezone to match the existing fetch_OHLC style.
    """
    df = df.copy()

    if datetime_col:
        if datetime_col not in df.columns:
            raise ValueError(f"datetime_col '{datetime_col}' was not found in columns: {list(df.columns)}")
        dt = pd.to_datetime(df[datetime_col], errors="coerce")
        df = df.drop(columns=[datetime_col])
    else:
        dt = pd.to_datetime(df.index, errors="coerce")

    if dt.isna().any():
        bad_count = int(dt.isna().sum())
        raise ValueError(f"Datetime parsing failed for {bad_count} row(s). Check datetime format.")

    # DatetimeIndex can be tz-aware as a whole, or object series with mixed tz strings.
    dt_index = pd.DatetimeIndex(dt)

    if dt_index.tz is None:
        dt_index = dt_index.tz_localize(source_tz_if_naive).tz_convert(target_tz)
    else:
        dt_index = dt_index.tz_convert(target_tz)

    dt_index = dt_index.tz_localize(None)

    if round_to_minute:
        dt_index = dt_index.round("min")

    df.index = dt_index
    df = df.sort_index()
    return df


def load_ohlc_file(
    input_path: str | Path,
    datetime_col: Optional[str] = None,
    message_col: str = "Message",
    source_tz_if_naive: str = "UTC",
    target_tz: str = "Asia/Bangkok",
    round_to_minute: bool = False,
) -> pd.DataFrame:
    """Load CSV/XLSX and return clean OHLC DataFrame indexed by naive Bangkok datetime."""
    input_path = Path(input_path)
    suffix = input_path.suffix.lower()

    if suffix == ".csv":
        # Same idea as current fetch_OHLC: first column is usually Datetime index.
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
    ohlc = normalize_ohlc_columns(raw, message_col=message_col)
    return ohlc


def slice_ohlc(df: pd.DataFrame, start: Optional[str], end: Optional[str]) -> pd.DataFrame:
    if start or end:
        df = df.loc[start:end].copy()
    if df.empty:
        raise ValueError(f"No OHLC rows found between start={start} and end={end}")
    return df


def render_with_mplfinance(df: pd.DataFrame, output_path: str | Path, title: str = "OHLC Chart") -> None:
    import mplfinance as mpf

    mpf_df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
    mpf.plot(
        mpf_df,
        type="candle",
        style="charles",
        title=title,
        ylabel="Price",
        volume=False,
        tight_layout=True,
        savefig=dict(fname=str(output_path), dpi=160, bbox_inches="tight"),
    )


def render_with_matplotlib(df: pd.DataFrame, output_path: str | Path, title: str = "OHLC Chart") -> None:
    """Pure matplotlib candlestick renderer, no mplfinance required."""
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(14, 7))
    x = mdates.date2num(df.index.to_pydatetime())

    if len(x) >= 2:
        candle_width = (x[1] - x[0]) * 0.65
    else:
        candle_width = 0.0005

    for xi, (_, row) in zip(x, df.iterrows()):
        o = float(row["open"])
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])

        # Matplotlib default color cycle is used intentionally.
        body_bottom = min(o, c)
        body_height = abs(c - o)
        if body_height == 0:
            body_height = max((h - l) * 0.01, 0.00001)

        line_color = "C2" if c >= o else "C3"
        ax.vlines(xi, l, h, linewidth=1.0, color=line_color)
        rect = Rectangle(
            (xi - candle_width / 2, body_bottom),
            candle_width,
            body_height,
            linewidth=1.0,
            edgecolor=line_color,
            facecolor=line_color,
            alpha=0.85,
        )
        ax.add_patch(rect)

    ax.set_title(title)
    ax.set_ylabel("Price")
    ax.grid(True, linewidth=0.4, alpha=0.35)
    ax.xaxis_date()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%y-%m-%d\n%H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path, dpi=160, bbox_inches="tight")
    plt.close(fig)


def render_ohlc_image(
    input_path: str | Path,
    output_path: str | Path,
    start: Optional[str] = None,
    end: Optional[str] = None,
    title: str = "OHLC Chart",
    datetime_col: Optional[str] = None,
    message_col: str = "Message",
    source_tz_if_naive: str = "UTC",
    target_tz: str = "Asia/Bangkok",
    prefer_mplfinance: bool = True,
    round_to_minute: bool = False,
) -> pd.DataFrame:
    df = load_ohlc_file(
        input_path=input_path,
        datetime_col=datetime_col,
        message_col=message_col,
        source_tz_if_naive=source_tz_if_naive,
        target_tz=target_tz,
        round_to_minute=round_to_minute,
    )
    df = slice_ohlc(df, start=start, end=end)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if prefer_mplfinance:
        try:
            render_with_mplfinance(df, output_path, title=title)
        except ImportError:
            render_with_matplotlib(df, output_path, title=title)
    else:
        render_with_matplotlib(df, output_path, title=title)

    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Render OHLC CSV/XLSX to candlestick image.")
    parser.add_argument("--input", required=True, help="Path to OHLC .csv/.xlsx/.xls file")
    parser.add_argument("--output", required=True, help="Output image path, e.g. chart.png or chart.jpg")
    parser.add_argument("--start", default=None, help="Start datetime after timezone conversion, e.g. 2026-04-24 20:00")
    parser.add_argument("--end", default=None, help="End datetime after timezone conversion, e.g. 2026-04-24 21:00")
    parser.add_argument("--title", default="OHLC Chart")
    parser.add_argument("--datetime-col", default=None, help="Use this if Datetime is a normal column, not the first/index column")
    parser.add_argument("--message-col", default="Message")
    parser.add_argument("--source-tz-if-naive", default="UTC")
    parser.add_argument("--target-tz", default="Asia/Bangkok")
    parser.add_argument("--matplotlib-only", action="store_true", help="Do not try mplfinance; use pure matplotlib")
    parser.add_argument("--round-to-minute", action="store_true", help="Round datetime index to nearest minute")
    args = parser.parse_args()

    df = render_ohlc_image(
        input_path=args.input,
        output_path=args.output,
        start=args.start,
        end=args.end,
        title=args.title,
        datetime_col=args.datetime_col,
        message_col=args.message_col,
        source_tz_if_naive=args.source_tz_if_naive,
        target_tz=args.target_tz,
        prefer_mplfinance=not args.matplotlib_only,
        round_to_minute=args.round_to_minute,
    )

    print(f"Rendered {len(df)} bars -> {os.path.abspath(args.output)}")
    print(f"First bar: {df.index[0]} | Last bar: {df.index[-1]}")


if __name__ == "__main__":
    main()
