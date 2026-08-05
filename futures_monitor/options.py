from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import re
from typing import Iterable

import numpy as np
import pandas as pd

from .config import Contract


@dataclass(frozen=True)
class OptionSource:
    exchange: str
    symbol: str
    function_name: str


OPTION_SOURCES: dict[str, OptionSource] = {
    "RB": OptionSource("SHFE", "螺纹钢期权", "option_hist_shfe"),
    "CU": OptionSource("SHFE", "铜期权", "option_hist_shfe"),
    "AL": OptionSource("SHFE", "铝期权", "option_hist_shfe"),
    "ZN": OptionSource("SHFE", "锌期权", "option_hist_shfe"),
    "AU": OptionSource("SHFE", "黄金期权", "option_hist_shfe"),
    "AG": OptionSource("SHFE", "白银期权", "option_hist_shfe"),
    "RU": OptionSource("SHFE", "天胶期权", "option_hist_shfe"),
    "I": OptionSource("DCE", "铁矿石期权", "option_hist_dce"),
    "PP": OptionSource("DCE", "聚丙烯期权", "option_hist_dce"),
    "L": OptionSource("DCE", "聚乙烯期权", "option_hist_dce"),
    "M": OptionSource("DCE", "豆粕期权", "option_hist_dce"),
    "Y": OptionSource("DCE", "豆油期权", "option_hist_dce"),
    "P": OptionSource("DCE", "棕榈油期权", "option_hist_dce"),
    "SR": OptionSource("CZCE", "白糖期权", "option_hist_czce"),
    "CF": OptionSource("CZCE", "棉花期权", "option_hist_czce"),
    "TA": OptionSource("CZCE", "PTA期权", "option_hist_czce"),
    "MA": OptionSource("CZCE", "甲醇期权", "option_hist_czce"),
}


def analyze_options_for_contracts(provider_name: str, contracts: Iterable[Contract]) -> dict[str, dict[str, object]]:
    if provider_name != "akshare":
        return {contract.symbol: unavailable_option_result(contract, "样例模式不生成期权结论。") for contract in contracts}

    import akshare as ak  # type: ignore

    return {contract.symbol: analyze_contract_option_structure(ak, contract) for contract in contracts}


def unavailable_option_result(contract: Contract, status: str) -> dict[str, object]:
    return {
        "available": False,
        "symbol": contract.symbol,
        "name": contract.name,
        "status": status,
        "source": "",
        "trade_date": "",
        "call_wall": None,
        "call_wall_oi": None,
        "put_wall": None,
        "put_wall_oi": None,
        "call_oi_change": None,
        "put_oi_change": None,
        "atm_iv": None,
        "skew_25d": None,
        "skew_10d": None,
        "summary": status,
    }


def analyze_contract_option_structure(ak, contract: Contract, lookback_days: int = 10) -> dict[str, object]:
    code = (contract.inventory_code or contract.symbol.rstrip("0")).upper()
    source = OPTION_SOURCES.get(code)
    if source is None:
        return unavailable_option_result(contract, "暂未配置该品种的国内商品期权公开接口。")

    last_error = ""
    for trade_date in _candidate_trade_dates(lookback_days):
        try:
            frame = getattr(ak, source.function_name)(symbol=source.symbol, trade_date=trade_date)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        if frame is None or frame.empty:
            continue
        normalized = normalize_option_table(frame)
        if normalized.empty:
            last_error = "接口返回数据缺少合约代码、持仓量或行权价。"
            continue
        result = analyze_option_table(contract, normalized, source, trade_date)
        result["status"] = "已接入国内交易所公开期权日频数据。"
        return result

    detail = f"最近{lookback_days}天没有拿到可用期权链。"
    if last_error:
        detail += f" 最近错误：{last_error}"
    return unavailable_option_result(contract, detail)


def normalize_option_table(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()

    renamed = frame.rename(
        columns={
            "合约代码": "contract",
            "持仓量": "open_interest",
            "持仓量变化": "open_interest_change",
            "增减量": "open_interest_change",
            "成交量": "volume",
            "成交量(手)": "volume",
            "隐含波动率": "iv",
            "德尔塔": "delta",
            "DELTA": "delta",
        }
    )
    if "contract" not in renamed.columns or "open_interest" not in renamed.columns:
        return pd.DataFrame()

    out = renamed.copy()
    parsed = out["contract"].astype(str).map(_parse_option_contract)
    out["option_type"] = parsed.map(lambda item: item[0])
    out["strike"] = parsed.map(lambda item: item[1])
    for col in ["open_interest", "open_interest_change", "volume", "iv", "delta", "strike"]:
        if col not in out.columns:
            out[col] = np.nan
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["iv"] = _normalize_iv(out["iv"])
    return out.dropna(subset=["option_type", "strike", "open_interest"])


def analyze_option_table(
    contract: Contract, table: pd.DataFrame, source: OptionSource, trade_date: str
) -> dict[str, object]:
    calls = table[table["option_type"] == "C"]
    puts = table[table["option_type"] == "P"]
    call_wall = _wall(calls)
    put_wall = _wall(puts)
    call_oi_change = _sum_or_none(calls["open_interest_change"])
    put_oi_change = _sum_or_none(puts["open_interest_change"])
    atm_iv = _atm_iv(table)
    skew_25d = _skew(table, 0.25)
    skew_10d = _skew(table, 0.10)
    summary = _option_summary(call_wall, put_wall, call_oi_change, put_oi_change, atm_iv, skew_25d, skew_10d)

    return {
        "available": True,
        "symbol": contract.symbol,
        "name": contract.name,
        "status": "",
        "source": f"{source.exchange} {source.symbol} / AkShare {source.function_name}",
        "trade_date": trade_date,
        "call_wall": call_wall["strike"] if call_wall else None,
        "call_wall_oi": call_wall["open_interest"] if call_wall else None,
        "put_wall": put_wall["strike"] if put_wall else None,
        "put_wall_oi": put_wall["open_interest"] if put_wall else None,
        "call_oi_change": call_oi_change,
        "put_oi_change": put_oi_change,
        "atm_iv": atm_iv,
        "skew_25d": skew_25d,
        "skew_10d": skew_10d,
        "summary": summary,
    }


def _candidate_trade_dates(lookback_days: int) -> list[str]:
    today = date.today()
    return [(today - timedelta(days=offset)).strftime("%Y%m%d") for offset in range(0, lookback_days)]


def _parse_option_contract(value: str) -> tuple[str | None, float | None]:
    match = re.search(r"([CP])(\d+(?:\.\d+)?)$", value.upper())
    if not match:
        return None, None
    return match.group(1), float(match.group(2))


def _normalize_iv(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    median = numeric.dropna().median()
    if pd.notna(median) and median > 2:
        return numeric / 100
    return numeric


def _wall(frame: pd.DataFrame) -> dict[str, float] | None:
    if frame.empty:
        return None
    grouped = frame.groupby("strike", as_index=False)["open_interest"].sum()
    if grouped.empty:
        return None
    row = grouped.sort_values("open_interest", ascending=False).iloc[0]
    return {"strike": float(row["strike"]), "open_interest": float(row["open_interest"])}


def _sum_or_none(series: pd.Series) -> float | None:
    usable = pd.to_numeric(series, errors="coerce").dropna()
    if usable.empty:
        return None
    return float(usable.sum())


def _atm_iv(frame: pd.DataFrame) -> float | None:
    usable = frame.dropna(subset=["iv", "delta"]).copy()
    if usable.empty:
        return None
    usable["atm_distance"] = (usable["delta"].abs() - 0.5).abs()
    nearest = usable.sort_values("atm_distance").head(4)
    if nearest.empty:
        return None
    return float(nearest["iv"].mean())


def _skew(frame: pd.DataFrame, target_delta: float) -> float | None:
    usable = frame.dropna(subset=["iv", "delta"]).copy()
    calls = usable[usable["option_type"] == "C"].copy()
    puts = usable[usable["option_type"] == "P"].copy()
    if calls.empty or puts.empty:
        return None
    calls["distance"] = (calls["delta"].abs() - target_delta).abs()
    puts["distance"] = (puts["delta"].abs() - target_delta).abs()
    call_iv = calls.sort_values("distance").iloc[0]["iv"]
    put_iv = puts.sort_values("distance").iloc[0]["iv"]
    if pd.isna(call_iv) or pd.isna(put_iv):
        return None
    return float(put_iv - call_iv)


def _option_summary(
    call_wall: dict[str, float] | None,
    put_wall: dict[str, float] | None,
    call_oi_change: float | None,
    put_oi_change: float | None,
    atm_iv: float | None,
    skew_25d: float | None,
    skew_10d: float | None,
) -> str:
    parts: list[str] = []
    if call_wall:
        parts.append(f"Call Wall {call_wall['strike']:.0f}，持仓{call_wall['open_interest']:.0f}手")
    if put_wall:
        parts.append(f"Put Wall {put_wall['strike']:.0f}，持仓{put_wall['open_interest']:.0f}手")
    if call_oi_change is not None and put_oi_change is not None:
        if call_oi_change > put_oi_change:
            parts.append(f"Call净增仓强于Put（{call_oi_change:.0f} / {put_oi_change:.0f}）")
        elif put_oi_change > call_oi_change:
            parts.append(f"Put净增仓强于Call（{put_oi_change:.0f} / {call_oi_change:.0f}）")
        else:
            parts.append("Call与Put净增仓接近")
    if atm_iv is not None:
        parts.append(f"ATM IV {atm_iv:.1%}")
    if skew_25d is not None:
        parts.append(f"25D Skew {skew_25d:.1%}")
    if skew_10d is not None:
        parts.append(f"10D Skew {skew_10d:.1%}")
    return "；".join(parts) if parts else "期权链已接入，但关键结构字段不足。"
