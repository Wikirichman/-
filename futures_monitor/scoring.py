from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import Contract, MonitorConfig
from .intraday import analyze_hourly


@dataclass(frozen=True)
class Selection:
    rank: int
    symbol: str
    name: str
    sector: str
    direction: str
    score: float
    tape_score: float
    supply_demand_score: float
    positioning_score: float
    long_score: float
    short_score: float
    return_20d: float
    return_60d: float
    inventory_30d: float | None
    external_inventory_30d: float | None
    oi_20d: float | None
    volume_ratio_10d: float
    hourly_direction: str | None
    hourly_setup: str | None
    hourly_trigger_level: float | None
    hourly_strength: float
    external_market: str | None
    external_return_20d: float | None
    supply_demand_analysis: str
    reason: str


def select_leaders(
    config: MonitorConfig,
    bars_by_symbol: dict[str, pd.DataFrame],
    inventory_by_symbol: dict[str, pd.DataFrame],
    hourly_by_symbol: dict[str, pd.DataFrame] | None = None,
    external_inventory_by_symbol: dict[str, pd.DataFrame] | None = None,
    external_bars_by_symbol: dict[str, pd.DataFrame] | None = None,
) -> list[Selection]:
    rows = []
    contract_by_symbol = {contract.symbol: contract for contract in config.contracts}
    for symbol, bars in bars_by_symbol.items():
        if len(bars) < 65:
            continue
        contract = contract_by_symbol[symbol]
        rows.append(
            _contract_features(
                contract,
                bars,
                inventory_by_symbol.get(symbol),
                hourly_by_symbol.get(symbol) if hourly_by_symbol else None,
                external_inventory_by_symbol.get(symbol) if external_inventory_by_symbol else None,
                external_bars_by_symbol.get(symbol) if external_bars_by_symbol else None,
                config.intraday,
            )
        )

    frame = pd.DataFrame(rows)
    if frame.empty:
        return []

    for col in [
        "long_tape_raw",
        "short_tape_raw",
        "long_supply_raw",
        "short_supply_raw",
        "long_positioning_raw",
        "short_positioning_raw",
        "liquidity_raw",
    ]:
        frame[f"{col}_z"] = _zscore(frame[col])

    weights = {"tape": 0.55, "supply_demand": 0.30, "positioning": 0.15} | config.weights
    frame["long_score"] = (
        weights["tape"] * frame["long_tape_raw_z"]
        + weights["supply_demand"] * frame["long_supply_raw_z"]
        + weights["positioning"] * frame["long_positioning_raw_z"]
        + float(config.intraday.get("weight", 0.25)) * frame["long_entry_raw"]
    )
    frame["short_score"] = (
        weights["tape"] * frame["short_tape_raw_z"]
        + weights["supply_demand"] * frame["short_supply_raw_z"]
        + weights["positioning"] * frame["short_positioning_raw_z"]
        + float(config.intraday.get("weight", 0.25)) * frame["short_entry_raw"]
    )
    frame["direction"] = np.where(frame["long_score"] >= frame["short_score"], "多头", "空头")
    frame["score"] = np.maximum(frame["long_score"], frame["short_score"]) + 0.08 * frame["liquidity_raw_z"]
    frame = frame[frame["liquidity_raw_z"] >= config.min_liquidity_score]
    if bool(config.supply_demand.get("required_for_selection", True)):
        frame = frame[
            frame["inventory_30d"].isna()
            | ((frame["direction"] == "多头") & (frame["inventory_30d"] <= 0))
            | ((frame["direction"] == "空头") & (frame["inventory_30d"] >= 0))
        ]
    if bool(config.positioning.get("required_for_selection", True)):
        minimum_oi = float(config.positioning.get("min_oi_change_20d", 0.05))
        frame = frame[frame["oi_20d"] >= minimum_oi]
    if bool(config.intraday.get("required_for_selection", False)):
        frame = frame[
            ((frame["direction"] == "多头") & (frame["long_entry_raw"] > 0))
            | ((frame["direction"] == "空头") & (frame["short_entry_raw"] > 0))
        ]
    frame = frame.sort_values("score", ascending=False).head(config.top_n).reset_index(drop=True)

    selections: list[Selection] = []
    for idx, row in frame.iterrows():
        selections.append(
            Selection(
                rank=idx + 1,
                symbol=row["symbol"],
                name=row["name"],
                sector=row["sector"],
                direction=row["direction"],
                score=round(float(row["score"]), 3),
                tape_score=round(float(_directional(row, "tape")), 3),
                supply_demand_score=round(float(_directional(row, "supply")), 3),
                positioning_score=round(float(_directional(row, "positioning")), 3),
                long_score=round(float(row["long_score"]), 3),
                short_score=round(float(row["short_score"]), 3),
                return_20d=float(row["return_20d"]),
                return_60d=float(row["return_60d"]),
                inventory_30d=_none_if_nan(row["inventory_30d"]),
                external_inventory_30d=_none_if_nan(row["external_inventory_30d"]),
                oi_20d=_none_if_nan(row["oi_20d"]),
                volume_ratio_10d=float(row["volume_ratio_10d"]),
                hourly_direction=_none_if_missing(row["hourly_direction"]),
                hourly_setup=_none_if_missing(row["hourly_setup"]),
                hourly_trigger_level=_none_if_nan(row["hourly_trigger_level"]),
                hourly_strength=float(row["hourly_strength"]),
                external_market=_none_if_missing(row["external_market"]),
                external_return_20d=_none_if_nan(row["external_return_20d"]),
                supply_demand_analysis=_supply_demand_analysis(row),
                reason=_reason(row),
            )
        )
    return selections


def selections_to_frame(selections: list[Selection]) -> pd.DataFrame:
    return pd.DataFrame([selection.__dict__ for selection in selections])


def _contract_features(
    contract: Contract,
    bars: pd.DataFrame,
    inventory: pd.DataFrame | None,
    hourly: pd.DataFrame | None,
    external_inventory: pd.DataFrame | None,
    external_bars: pd.DataFrame | None,
    intraday_config: dict[str, float | int | bool],
) -> dict[str, object]:
    bars = bars.sort_values("date").copy()
    close = bars["close"]
    ret_20 = close.iloc[-1] / close.iloc[-21] - 1
    ret_60 = close.iloc[-1] / close.iloc[-61] - 1
    ma_20 = close.rolling(20).mean().iloc[-1]
    ma_60 = close.rolling(60).mean().iloc[-1]
    ma_gap = ma_20 / ma_60 - 1 if ma_60 else 0.0
    high_60 = bars["high"].rolling(60).max().iloc[-1]
    low_60 = bars["low"].rolling(60).min().iloc[-1]
    breakout = (close.iloc[-1] - low_60) / (high_60 - low_60) - 0.5 if high_60 > low_60 else 0
    volume_ratio = _safe_ratio(bars["volume"].tail(10).mean(), bars["volume"].tail(60).mean()) - 1

    oi = bars["open_interest"].dropna()
    oi_20 = float(oi.iloc[-1] / oi.iloc[-21] - 1) if len(oi) >= 21 and oi.iloc[-21] else np.nan

    inv_30 = np.nan
    if inventory is not None and not inventory.empty and len(inventory) >= 31:
        inv = inventory.sort_values("date")["inventory"]
        inv_30 = float(inv.iloc[-1] / inv.iloc[-31] - 1) if inv.iloc[-31] else np.nan

    external_inv_30 = np.nan
    if external_inventory is not None and not external_inventory.empty and len(external_inventory) >= 31:
        ext_inv = external_inventory.sort_values("date")["inventory"]
        external_inv_30 = float(ext_inv.iloc[-1] / ext_inv.iloc[-31] - 1) if ext_inv.iloc[-31] else np.nan

    external_ret_20 = np.nan
    if external_bars is not None and not external_bars.empty and len(external_bars) >= 21:
        ext_close = external_bars.sort_values("date")["close"]
        external_ret_20 = float(ext_close.iloc[-1] / ext_close.iloc[-21] - 1) if ext_close.iloc[-21] else np.nan

    long_tape_raw = 0.45 * ret_20 + 0.25 * ret_60 + 0.20 * ma_gap + 0.10 * breakout
    short_tape_raw = -long_tape_raw
    domestic_long_supply = -inv_30 if not np.isnan(inv_30) else np.nan
    external_long_supply = -external_inv_30 if not np.isnan(external_inv_30) else np.nan
    long_supply_raw = _mean_or_zero([domestic_long_supply, external_long_supply])
    short_supply_raw = _mean_or_zero(
        [
            inv_30 if not np.isnan(inv_30) else np.nan,
            external_inv_30 if not np.isnan(external_inv_30) else np.nan,
        ]
    )
    oi_confirmation = oi_20 if not np.isnan(oi_20) and oi_20 > 0 else 0.0
    long_positioning_raw = oi_confirmation if ret_20 > 0 else -oi_confirmation
    short_positioning_raw = oi_confirmation if ret_20 < 0 else -oi_confirmation
    long_positioning_raw += 0.3 * volume_ratio if ret_20 > 0 else -0.15 * volume_ratio
    short_positioning_raw += 0.3 * volume_ratio if ret_20 < 0 else -0.15 * volume_ratio
    liquidity_raw = np.log1p(float(bars["volume"].tail(20).mean()))
    hourly_signal = analyze_hourly(
        hourly,
        swing_window=int(intraday_config.get("swing_window", 2)),
        range_lookback=int(intraday_config.get("range_lookback", 16)),
        max_range_atr=float(intraday_config.get("max_range_atr", 6.0)),
        min_breakout_atr=float(intraday_config.get("min_breakout_atr", 0.10)),
        min_breakout_volume_ratio=float(intraday_config.get("min_breakout_volume_ratio", 1.0)),
    )

    return {
        "symbol": contract.symbol,
        "name": contract.name,
        "sector": contract.sector,
        "return_20d": ret_20,
        "return_60d": ret_60,
        "inventory_30d": inv_30,
        "external_inventory_30d": external_inv_30,
        "oi_20d": oi_20,
        "volume_ratio_10d": volume_ratio,
        "external_market": contract.external_market,
        "external_return_20d": external_ret_20,
        "long_tape_raw": long_tape_raw,
        "short_tape_raw": short_tape_raw,
        "long_supply_raw": long_supply_raw,
        "short_supply_raw": short_supply_raw,
        "long_positioning_raw": long_positioning_raw,
        "short_positioning_raw": short_positioning_raw,
        "long_entry_raw": hourly_signal.long_score,
        "short_entry_raw": hourly_signal.short_score,
        "hourly_setup": hourly_signal.setup,
        "hourly_direction": hourly_signal.direction,
        "hourly_trigger_level": hourly_signal.trigger_level,
        "hourly_strength": hourly_signal.strength,
        "liquidity_raw": liquidity_raw,
    }


def _zscore(series: pd.Series) -> pd.Series:
    std = series.std(ddof=0)
    if not std or np.isnan(std):
        return pd.Series(np.zeros(len(series)), index=series.index)
    return (series - series.mean()) / std


def _safe_ratio(a: float, b: float) -> float:
    if b == 0 or np.isnan(b):
        return 1.0
    return float(a / b)


def _none_if_nan(value: object) -> float | None:
    if value is None:
        return None
    value = float(value)
    return None if np.isnan(value) else value


def _none_if_missing(value: object) -> str | None:
    return None if value is None or pd.isna(value) else str(value)


def _mean_or_zero(values: list[float]) -> float:
    usable = [value for value in values if not np.isnan(value)]
    if not usable:
        return 0.0
    return float(np.mean(usable))


def _directional(row: pd.Series, factor: str) -> float:
    prefix = "long" if row["direction"] == "多头" else "short"
    return float(row[f"{prefix}_{factor}_raw_z"])


def _supply_demand_analysis(row: pd.Series) -> str:
    direction = str(row["direction"])
    market = row["external_market"]
    market = None if pd.isna(market) else str(market)
    domestic = row["inventory_30d"]
    external = row["external_inventory_30d"]
    external_ret = row["external_return_20d"]
    parts = []

    if pd.isna(domestic):
        parts.append("国内库存暂缺")
    elif float(domestic) < 0:
        parts.append(f"国内库存30日下降{-float(domestic):.1%}")
    else:
        parts.append(f"国内库存30日增加{float(domestic):.1%}")

    if market and not pd.isna(external):
        if float(external) < 0:
            parts.append(f"{market}库存30日下降{-float(external):.1%}")
        else:
            parts.append(f"{market}库存30日增加{float(external):.1%}")
    elif market:
        parts.append(f"{market}库存暂缺")

    if market and not pd.isna(external_ret):
        if float(external_ret) >= 0:
            parts.append(f"{market}价格20日上涨{float(external_ret):.1%}")
        else:
            parts.append(f"{market}价格20日下跌{-float(external_ret):.1%}")

    domestic_support = (
        None if pd.isna(domestic) else (float(domestic) < 0 if direction == "多头" else float(domestic) > 0)
    )
    external_support = (
        None if pd.isna(external) else (float(external) < 0 if direction == "多头" else float(external) > 0)
    )
    price_support = (
        None
        if pd.isna(external_ret)
        else (float(external_ret) > 0 if direction == "多头" else float(external_ret) < 0)
    )

    if market:
        if domestic_support is True and external_support is True and price_support is True:
            conclusion = "内外盘库存与价格共振，供需方向一致。"
        elif external_support is True and price_support is True and domestic_support is None:
            conclusion = "海外库存与价格支持当前方向，国内库存仍待补充。"
        elif domestic_support is True and external_support is False:
            conclusion = "国内与海外库存方向分化，当前以国内盘面为主。"
        elif domestic_support is False and external_support is True:
            conclusion = "海外支持但国内库存不配合，供需分歧仍在。"
        elif external_support is False and price_support is False:
            conclusion = "海外库存和价格都不支持当前方向，需警惕外盘掣肘。"
        else:
            conclusion = "内外盘供需线索不完全一致，保持客观跟踪。"
    else:
        conclusion = "当前按国内库存与盘面确认供需方向。"
    parts.append(conclusion)
    return "；".join(parts)


def _reason(row: pd.Series) -> str:
    parts = []
    direction = row["direction"]
    ret_20 = float(row["return_20d"])
    inv_30 = row["inventory_30d"]
    oi_20 = row["oi_20d"]
    volume_ratio = float(row["volume_ratio_10d"])
    hourly_direction = row["hourly_direction"]
    hourly_setup = row["hourly_setup"]
    hourly_trigger_level = row["hourly_trigger_level"]
    if direction == "多头":
        parts.append(f"20日涨幅{ret_20:.1%}" if ret_20 >= 0 else f"20日仍跌{-ret_20:.1%}，关注小时级别反转")
        if not np.isnan(inv_30):
            parts.append(f"库存30日下降{-float(inv_30):.1%}" if inv_30 < 0 else f"库存30日增加{float(inv_30):.1%}")
        if not np.isnan(oi_20):
            if oi_20 > 0 and ret_20 >= 0:
                parts.append(f"上涨增仓，持仓20日增加{float(oi_20):.1%}")
            elif oi_20 > 0:
                parts.append(f"持仓20日增加{float(oi_20):.1%}，配合小时反转观察多头确认")
            else:
                parts.append(f"上涨但持仓20日下降{-float(oi_20):.1%}")
    else:
        parts.append(f"20日跌幅{-ret_20:.1%}" if ret_20 <= 0 else f"20日仍涨{ret_20:.1%}，关注小时级别转弱")
        if not np.isnan(inv_30):
            parts.append(f"库存30日增加{float(inv_30):.1%}" if inv_30 > 0 else f"库存30日下降{-float(inv_30):.1%}")
        if not np.isnan(oi_20):
            if oi_20 > 0 and ret_20 <= 0:
                parts.append(f"下跌增仓，持仓20日增加{float(oi_20):.1%}")
            elif oi_20 > 0:
                parts.append(f"持仓20日增加{float(oi_20):.1%}，配合小时转弱观察空头确认")
            else:
                parts.append(f"下跌但持仓20日下降{-float(oi_20):.1%}")
    parts.append(f"近10日成交较60日均值变化{volume_ratio:.1%}")
    if hourly_setup is not None and not pd.isna(hourly_setup) and hourly_direction == direction:
        parts.append(f"小时线{hourly_setup}，触发位{float(hourly_trigger_level):.2f}")
    elif hourly_setup is not None and not pd.isna(hourly_setup):
        parts.append(f"小时线出现{hourly_setup}，但与日线方向不一致，未计入触发确认")
    else:
        parts.append("小时线暂未出现N字反转或中枢突破")
    parts.append("方向化盘面、库存与持仓确认综合排名靠前")
    return "；".join(parts)
