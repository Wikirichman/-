from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class IntradaySignal:
    direction: str | None
    setup: str | None
    strength: float
    trigger_level: float | None

    @property
    def long_score(self) -> float:
        return self.strength if self.direction == "多头" else 0.0

    @property
    def short_score(self) -> float:
        return self.strength if self.direction == "空头" else 0.0


def analyze_hourly(
    bars: pd.DataFrame | None,
    *,
    swing_window: int = 2,
    range_lookback: int = 16,
    max_range_atr: float = 6.0,
    min_breakout_atr: float = 0.10,
    min_breakout_volume_ratio: float = 1.0,
) -> IntradaySignal:
    """Find an hourly N reversal or a breakout from a compact trading range."""
    if bars is None or len(bars) < max(2 * swing_window + 8, range_lookback + 2):
        return IntradaySignal(None, None, 0.0, None)

    frame = bars.sort_values("date").reset_index(drop=True).copy()
    required = {"high", "low", "close"}
    if not required.issubset(frame.columns):
        return IntradaySignal(None, None, 0.0, None)

    atr = _atr(frame)
    if not np.isfinite(atr) or atr <= 0:
        return IntradaySignal(None, None, 0.0, None)

    close = float(frame["close"].iloc[-1])
    volume_ratio = _volume_ratio(frame)
    n_signal = _n_reversal(frame, swing_window, close, atr, min_breakout_atr)
    range_signal = _range_breakout(
        frame,
        close,
        atr,
        range_lookback,
        max_range_atr,
        min_breakout_atr,
        volume_ratio,
        min_breakout_volume_ratio,
    )

    # A structure reversal is stronger than a generic range escape. When both agree,
    # keep the N setup and elevate its confidence.
    if n_signal.direction and range_signal.direction == n_signal.direction:
        return IntradaySignal(n_signal.direction, n_signal.setup, 1.25, n_signal.trigger_level)
    if n_signal.direction:
        return n_signal
    return range_signal


def _n_reversal(
    frame: pd.DataFrame, swing_window: int, close: float, atr: float, min_breakout_atr: float
) -> IntradaySignal:
    swings = _swings(frame, swing_window)
    if len(swings) < 3:
        return IntradaySignal(None, None, 0.0, None)

    # Bullish N: a higher low forms after a swing high, then price closes through
    # that swing high. It encodes a local downtrend reversal without needing labels.
    for pos in range(len(swings) - 3, -1, -1):
        low_0, high_0, low_1 = swings[pos : pos + 3]
        if low_0[1] == "L" and high_0[1] == "H" and low_1[1] == "L":
            if low_1[2] > low_0[2] and close > high_0[2] + atr * min_breakout_atr:
                return IntradaySignal("多头", "N字反转突破摆动高点", 1.0, high_0[2])

    # Bearish N: a lower high forms after a swing low, then price closes through
    # that swing low.
    for pos in range(len(swings) - 3, -1, -1):
        high_0, low_0, high_1 = swings[pos : pos + 3]
        if high_0[1] == "H" and low_0[1] == "L" and high_1[1] == "H":
            if high_1[2] < high_0[2] and close < low_0[2] - atr * min_breakout_atr:
                return IntradaySignal("空头", "N字反转跌破摆动低点", 1.0, low_0[2])
    return IntradaySignal(None, None, 0.0, None)


def _range_breakout(
    frame: pd.DataFrame,
    close: float,
    atr: float,
    lookback: int,
    max_range_atr: float,
    min_breakout_atr: float,
    volume_ratio: float,
    min_volume_ratio: float,
) -> IntradaySignal:
    center = frame.iloc[-lookback - 1 : -1]
    if center.empty:
        return IntradaySignal(None, None, 0.0, None)
    ceiling = float(center["high"].max())
    floor = float(center["low"].min())
    compact = (ceiling - floor) / atr <= max_range_atr
    volume_confirmed = not np.isfinite(volume_ratio) or volume_ratio >= min_volume_ratio
    if compact and volume_confirmed and close > ceiling + atr * min_breakout_atr:
        return IntradaySignal("多头", "突破小时震荡中枢上沿", 0.8, ceiling)
    if compact and volume_confirmed and close < floor - atr * min_breakout_atr:
        return IntradaySignal("空头", "跌破小时震荡中枢下沿", 0.8, floor)
    return IntradaySignal(None, None, 0.0, None)


def _swings(frame: pd.DataFrame, window: int) -> list[tuple[int, str, float]]:
    highs = frame["high"].to_numpy(dtype=float)
    lows = frame["low"].to_numpy(dtype=float)
    points: list[tuple[int, str, float]] = []
    for index in range(window, len(frame) - window):
        high_window = highs[index - window : index + window + 1]
        low_window = lows[index - window : index + window + 1]
        if highs[index] == high_window.max() and np.count_nonzero(high_window == highs[index]) == 1:
            points.append((index, "H", float(highs[index])))
        if lows[index] == low_window.min() and np.count_nonzero(low_window == lows[index]) == 1:
            points.append((index, "L", float(lows[index])))
    return sorted(points, key=lambda item: item[0])


def _atr(frame: pd.DataFrame, period: int = 14) -> float:
    high = frame["high"]
    low = frame["low"]
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [(high - low).abs(), (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)
    return float(true_range.tail(period).mean())


def _volume_ratio(frame: pd.DataFrame) -> float:
    if "volume" not in frame.columns or len(frame) < 12:
        return np.nan
    baseline = frame["volume"].iloc[-11:-1].mean()
    if baseline <= 0:
        return np.nan
    return float(frame["volume"].iloc[-1] / baseline)
