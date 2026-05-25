"""
ohlc_image_rendering_bw.py

Standalone OHLC image renderer for David project.
Version: 2026-05-12-bw-window-size-fixed

Purpose:
- Render OHLC chart images for AI model training.
- Bull bar: white solid body with black border.
- Bear bar: black solid body with black border.
- Wick is NOT drawn through the body.
- Grid is OFF by default.
- This version intentionally uses matplotlib only to avoid mplfinance style fallback issues.

Install:
    pip install pandas matplotlib openpyxl

Example:
    python ohlc_image_rendering_bw.py ^
      --input "C:\\david\\ohlc\\XAUUSD-Go Markets-1m.csv" ^
      --output "C:\\david\\test\\XAUUSD_bw.png" ^
      --start "2026-04-24 20:00" ^
      --end "2026-04-24 21:00" ^
      --title "XAUUSD 1m"
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Optional

import pandas as pd

VERSION = "2026-05-12-bw-window-size-fixed"


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
    """Slice OHLC dataframe by either datetime range or fixed bar window.

    Rules:
    - window_size <= 0: use start/end datetime range.
    - window_size > 0: use start datetime as anchor and take exactly N bars from there.
      In this mode, end datetime is intentionally ignored.
    """
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


def render_bw_candles(
    df: pd.DataFrame,
    output_path: str | Path,
    title: str = "OHLC Chart",
    show_grid: bool = False,
    hide_axes: bool = False,
    dpi: int = 160,
) -> None:
    """Render black/white candlesticks using pure matplotlib only.

    Drawing order:
    1) Draw wick only above and below the candle body.
    2) Draw candle body on top.

    This prevents wick lines from cutting through white bull bodies.
    """
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(14, 7))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    x = mdates.date2num(df.index.to_pydatetime())
    if len(x) >= 2:
        candle_width = (x[1] - x[0]) * 0.65
    else:
        candle_width = 0.0005

    price_range = float(df["high"].max() - df["low"].min())
    min_body = max(price_range * 0.001, 1e-8)

    for xi, row in zip(x, df.itertuples(index=False)):
        o = float(row.open)
        h = float(row.high)
        l = float(row.low)
        c = float(row.close)

        body_bottom = min(o, c)
        body_top = max(o, c)
        body_height = body_top - body_bottom

        if body_height == 0:
            body_bottom = o - min_body / 2
            body_top = o + min_body / 2
            body_height = min_body

        is_bull = c >= o
        face_color = "white" if is_bull else "black"

        # Wick segments: never draw through the body.
        if h > body_top:
            ax.vlines(xi, body_top, h, linewidth=1.0, color="black", zorder=1)
        if l < body_bottom:
            ax.vlines(xi, l, body_bottom, linewidth=1.0, color="black", zorder=1)

        body = Rectangle(
            (xi - candle_width / 2, body_bottom),
            candle_width,
            body_height,
            linewidth=1.0,
            edgecolor="black",
            facecolor=face_color,
            zorder=2,
        )
        ax.add_patch(body)

    ax.set_xlim(x[0] - candle_width, x[-1] + candle_width)
    y_pad = price_range * 0.05 if price_range > 0 else 1
    ax.set_ylim(float(df["low"].min()) - y_pad, float(df["high"].max()) + y_pad)

    if show_grid:
        ax.grid(True, linewidth=0.4, alpha=0.25)
    else:
        ax.grid(False)

    if hide_axes:
        ax.set_axis_off()
    else:
        ax.set_title(title)
        ax.set_ylabel("Price")
        ax.xaxis_date()
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%y-%m-%d\n%H:%M"))
        fig.autofmt_xdate()

    fig.tight_layout()
    fig.savefig(output_path, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def render_ohlc_image(
    input_path: str | Path,
    output_path: str | Path,
    start: Optional[str] = None,
    end: Optional[str] = None,
    window_size: int = 0,
    title: str = "OHLC Chart",
    datetime_col: Optional[str] = None,
    message_col: str = "Message",
    source_tz_if_naive: str = "UTC",
    target_tz: str = "Asia/Bangkok",
    round_to_minute: bool = False,
    show_grid: bool = False,
    hide_axes: bool = False,
    dpi: int = 160,
) -> pd.DataFrame:
    df = load_ohlc_file(
        input_path=input_path,
        datetime_col=datetime_col,
        message_col=message_col,
        source_tz_if_naive=source_tz_if_naive,
        target_tz=target_tz,
        round_to_minute=round_to_minute,
    )
    df = slice_ohlc(df, start=start, end=end, window_size=window_size)
    render_bw_candles(df, output_path, title=title, show_grid=show_grid, hide_axes=hide_axes, dpi=dpi)
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Render OHLC CSV/XLSX to black-white candlestick image.")
    parser.add_argument("--input", required=False, help="Path to OHLC .csv/.xlsx/.xls file")
    parser.add_argument("--output", required=False, help="Output image path, e.g. chart.png or chart.jpg")
    parser.add_argument("--start", default=None, help="Start datetime after timezone conversion, e.g. 2026-04-24 20:00")
    parser.add_argument("--end", default=None, help="End datetime after timezone conversion, e.g. 2026-04-24 21:00")
    parser.add_argument(
        "--window-size",
        type=int,
        default=0,
        help="Number of bars to render from --start. Default 0 uses --start/--end datetime range. If >0, --end is ignored.",
    )
    parser.add_argument("--title", default="OHLC Chart")
    parser.add_argument("--datetime-col", default=None, help="Use this if Datetime is a normal column, not first/index column")
    parser.add_argument("--message-col", default="Message")
    parser.add_argument("--source-tz-if-naive", default="UTC")
    parser.add_argument("--target-tz", default="Asia/Bangkok")
    parser.add_argument("--round-to-minute", action="store_true")
    parser.add_argument("--show-grid", action="store_true", help="Grid is off by default. Add this only for debug.")
    parser.add_argument("--hide-axes", action="store_true", help="Hide title, x/y axis, ticks. Useful for pure AI image training.")
    parser.add_argument("--dpi", type=int, default=160)
    parser.add_argument("--version", action="store_true", help="Print script version and exit")
    args = parser.parse_args()

    if args.version:
        print(VERSION)
        return

    if not args.input or not args.output:
        parser.error("--input and --output are required unless --version is used")

    df = render_ohlc_image(
        input_path=args.input,
        output_path=args.output,
        start=args.start,
        end=args.end,
        window_size=args.window_size,
        title=args.title,
        datetime_col=args.datetime_col,
        message_col=args.message_col,
        source_tz_if_naive=args.source_tz_if_naive,
        target_tz=args.target_tz,
        round_to_minute=args.round_to_minute,
        show_grid=args.show_grid,
        hide_axes=args.hide_axes,
        dpi=args.dpi,
    )

    print(f"ohlc_image_rendering_bw.py version={VERSION}")
    print("renderer=matplotlib-only")
    print("bull=white, bear=black, grid=" + ("on" if args.show_grid else "off"))
    if args.window_size > 0:
        print(f"window_size={args.window_size} (fixed-bar mode; --end ignored)")
    else:
        print("window_size=0 (datetime range mode; --start/--end used)")
    print(f"Rendered {len(df)} bars -> {os.path.abspath(args.output)}")
    print(f"First bar: {df.index[0]} | Last bar: {df.index[-1]}")


if __name__ == "__main__":
    main()
