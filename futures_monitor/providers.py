from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Iterable

import numpy as np
import pandas as pd

from .config import Contract


class DataProvider(ABC):
    @abstractmethod
    def daily_bars(self, contract: Contract, lookback_days: int) -> pd.DataFrame:
        """Return columns: date, open, high, low, close, volume, open_interest."""

    @abstractmethod
    def inventory(self, contract: Contract, lookback_days: int) -> pd.DataFrame:
        """Return columns: date, inventory. Empty data is acceptable."""

    @abstractmethod
    def hourly_bars(self, contract: Contract, lookback_bars: int) -> pd.DataFrame:
        """Return hourly columns: date, open, high, low, close, volume, open_interest."""

    @abstractmethod
    def external_inventory(self, contract: Contract, lookback_days: int) -> pd.DataFrame:
        """Return columns: date, inventory for mapped overseas markets."""

    @abstractmethod
    def external_daily_bars(self, contract: Contract, lookback_days: int) -> pd.DataFrame:
        """Return overseas daily bars for mapped contracts."""


class AkshareProvider(DataProvider):
    def __init__(self) -> None:
        import akshare as ak  # type: ignore

        self.ak = ak
        self._lme_stock_cache: pd.DataFrame | None = None

    def daily_bars(self, contract: Contract, lookback_days: int) -> pd.DataFrame:
        df = self.ak.futures_zh_daily_sina(symbol=contract.symbol)
        df = _normalize_daily(df)
        return df.tail(max(lookback_days, 65)).reset_index(drop=True)

    def inventory(self, contract: Contract, lookback_days: int) -> pd.DataFrame:
        for candidate in _inventory_candidates(contract):
            try:
                df = self.ak.futures_inventory_em(symbol=candidate)
                normalized = _normalize_inventory(df).tail(max(lookback_days, 65)).reset_index(drop=True)
                if not normalized.empty:
                    return normalized
            except Exception:
                continue
        if not contract.inventory_symbol:
            return pd.DataFrame(columns=["date", "inventory"])
        try:
            df = self.ak.futures_inventory_99(symbol=contract.inventory_symbol)
        except Exception:
            return pd.DataFrame(columns=["date", "inventory"])
        return _normalize_inventory(df).tail(max(lookback_days, 65)).reset_index(drop=True)

    def hourly_bars(self, contract: Contract, lookback_bars: int) -> pd.DataFrame:
        try:
            df = self.ak.futures_zh_minute_sina(symbol=contract.symbol, period="60")
        except Exception:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "open_interest"])
        return _normalize_daily(df).tail(lookback_bars).reset_index(drop=True)

    def external_inventory(self, contract: Contract, lookback_days: int) -> pd.DataFrame:
        if not contract.external_market or not contract.external_inventory_symbol:
            return pd.DataFrame(columns=["date", "inventory"])
        try:
            if contract.external_market == "COMEX":
                df = self.ak.futures_comex_inventory(symbol=contract.external_inventory_symbol)
                return _normalize_external_inventory(df).tail(max(lookback_days, 65)).reset_index(drop=True)
            if contract.external_market == "LME":
                if self._lme_stock_cache is None:
                    self._lme_stock_cache = self.ak.macro_euro_lme_stock()
                df = _extract_lme_inventory(self._lme_stock_cache, contract.external_inventory_symbol)
                return df.tail(max(lookback_days, 65)).reset_index(drop=True)
        except Exception:
            return pd.DataFrame(columns=["date", "inventory"])
        return pd.DataFrame(columns=["date", "inventory"])

    def external_daily_bars(self, contract: Contract, lookback_days: int) -> pd.DataFrame:
        if not contract.external_price_symbol:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "open_interest"])
        try:
            df = self.ak.futures_foreign_hist(symbol=contract.external_price_symbol)
        except Exception:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "open_interest"])
        return _normalize_daily(df).tail(max(lookback_days, 65)).reset_index(drop=True)


class SampleProvider(DataProvider):
    """Deterministic sample data for offline development and CI."""

    def __init__(self, seed: int = 7) -> None:
        self.seed = seed

    def daily_bars(self, contract: Contract, lookback_days: int) -> pd.DataFrame:
        rng = np.random.default_rng(abs(hash((contract.symbol, self.seed))) % (2**32))
        n = max(lookback_days, 120)
        dates = pd.bdate_range(end=date.today(), periods=n)
        drift = rng.normal(0.0008, 0.0012)
        if contract.symbol in {"CU0", "AG0", "P0"}:
            drift += 0.0018
        if contract.symbol in {"RB0", "HC0", "J0"}:
            drift -= 0.0012
        returns = rng.normal(drift, 0.018, size=n)
        close = 100 * np.cumprod(1 + returns)
        volume = rng.integers(50_000, 500_000, size=n) * (1 + np.linspace(0, 0.3, n))
        open_interest = rng.integers(80_000, 600_000, size=n) * (1 + np.linspace(0, 0.2, n))
        high = close * (1 + rng.uniform(0.002, 0.018, size=n))
        low = close * (1 - rng.uniform(0.002, 0.018, size=n))
        open_ = close * (1 + rng.normal(0, 0.006, size=n))
        return pd.DataFrame(
            {
                "date": dates,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "open_interest": open_interest,
            }
        )

    def inventory(self, contract: Contract, lookback_days: int) -> pd.DataFrame:
        if not contract.inventory_symbol:
            return pd.DataFrame(columns=["date", "inventory"])
        rng = np.random.default_rng(abs(hash((contract.inventory_symbol, self.seed))) % (2**32))
        n = max(lookback_days, 120)
        dates = pd.bdate_range(end=date.today(), periods=n)
        slope = rng.normal(0, 0.004)
        if contract.symbol in {"CU0", "AG0", "P0"}:
            slope -= 0.006
        if contract.symbol in {"RB0", "HC0", "J0"}:
            slope += 0.006
        changes = rng.normal(slope, 0.02, size=n)
        inventory = 1000 * np.cumprod(1 + changes)
        return pd.DataFrame({"date": dates, "inventory": inventory})

    def hourly_bars(self, contract: Contract, lookback_bars: int) -> pd.DataFrame:
        rng = np.random.default_rng(abs(hash(("hourly", contract.symbol, self.seed))) % (2**32))
        n = max(lookback_bars, 80)
        dates = pd.date_range(end=pd.Timestamp.now().floor("h"), periods=n, freq="h")
        close = 100 * np.cumprod(1 + rng.normal(0.0001, 0.004, size=n))
        high = close * (1 + rng.uniform(0.0005, 0.004, size=n))
        low = close * (1 - rng.uniform(0.0005, 0.004, size=n))
        open_ = close * (1 + rng.normal(0, 0.002, size=n))
        volume = rng.integers(3_000, 30_000, size=n)
        open_interest = rng.integers(80_000, 600_000, size=n)
        return pd.DataFrame(
            {
                "date": dates,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "open_interest": open_interest,
            }
        )

    def external_inventory(self, contract: Contract, lookback_days: int) -> pd.DataFrame:
        if not contract.external_market:
            return pd.DataFrame(columns=["date", "inventory"])
        rng = np.random.default_rng(abs(hash(("ext_inv", contract.symbol, self.seed))) % (2**32))
        n = max(lookback_days, 120)
        dates = pd.bdate_range(end=date.today(), periods=n)
        slope = rng.normal(0, 0.004)
        if contract.symbol in {"CU0", "AL0", "ZN0", "AU0"}:
            slope -= 0.004
        if contract.symbol in {"AG0"}:
            slope += 0.003
        changes = rng.normal(slope, 0.015, size=n)
        inventory = 800 * np.cumprod(1 + changes)
        return pd.DataFrame({"date": dates, "inventory": inventory})

    def external_daily_bars(self, contract: Contract, lookback_days: int) -> pd.DataFrame:
        if not contract.external_price_symbol:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "open_interest"])
        rng = np.random.default_rng(abs(hash(("ext_bar", contract.symbol, self.seed))) % (2**32))
        n = max(lookback_days, 120)
        dates = pd.bdate_range(end=date.today(), periods=n)
        drift = rng.normal(0.0004, 0.001)
        if contract.symbol in {"CU0", "AL0", "ZN0", "AU0"}:
            drift += 0.001
        returns = rng.normal(drift, 0.014, size=n)
        close = 100 * np.cumprod(1 + returns)
        return pd.DataFrame(
            {
                "date": dates,
                "open": close * (1 + rng.normal(0, 0.003, size=n)),
                "high": close * (1 + rng.uniform(0.001, 0.01, size=n)),
                "low": close * (1 - rng.uniform(0.001, 0.01, size=n)),
                "close": close,
                "volume": rng.integers(5_000, 50_000, size=n),
                "open_interest": np.nan,
            }
        )


def provider_from_name(name: str) -> DataProvider:
    if name == "akshare":
        return AkshareProvider()
    if name == "sample":
        return SampleProvider()
    raise ValueError(f"Unknown provider: {name}")


def fetch_universe(
    provider: DataProvider, contracts: Iterable[Contract], lookback_days: int, hourly_lookback_bars: int
) -> tuple[
    dict[str, pd.DataFrame],
    dict[str, pd.DataFrame],
    dict[str, pd.DataFrame],
    dict[str, pd.DataFrame],
    dict[str, pd.DataFrame],
]:
    bars: dict[str, pd.DataFrame] = {}
    inventories: dict[str, pd.DataFrame] = {}
    hourly: dict[str, pd.DataFrame] = {}
    external_inventories: dict[str, pd.DataFrame] = {}
    external_bars: dict[str, pd.DataFrame] = {}
    for contract in contracts:
        bars[contract.symbol] = provider.daily_bars(contract, lookback_days)
        inventories[contract.symbol] = provider.inventory(contract, lookback_days)
        hourly[contract.symbol] = provider.hourly_bars(contract, hourly_lookback_bars)
        external_inventories[contract.symbol] = provider.external_inventory(contract, lookback_days)
        external_bars[contract.symbol] = provider.external_daily_bars(contract, lookback_days)
    return bars, inventories, hourly, external_inventories, external_bars


def provider_source_manifest(provider_name: str, contracts: Iterable[Contract]) -> dict[str, object]:
    contract_list = list(contracts)
    if provider_name == "sample":
        return {
            "provider": "sample",
            "mode": "演示数据",
            "strict": False,
            "headline": "当前是离线样例数据，只用于界面联调和规则演示，不能用于实盘研究结论。",
            "policy": "样例模式下所有价格、库存和持仓都是程序生成值，不代表任何真实市场。",
            "items": [
                {"title": "国内日线 / 持仓 / 成交量", "detail": "SampleProvider 随机生成 OHLC、成交量和持仓量。"},
                {"title": "国内库存", "detail": "SampleProvider 生成 30 日库存变化序列。"},
                {"title": "小时线", "detail": "SampleProvider 生成 60 分钟级别价格与成交量。"},
                {"title": "外盘库存 / 外盘价格", "detail": "SampleProvider 生成 LME / COMEX 映射序列。"},
            ],
            "external_contracts": [contract.symbol for contract in contract_list if contract.external_market],
        }

    external_contracts = [contract for contract in contract_list if contract.external_market]
    external_labels = [f"{contract.symbol}({contract.external_market})" for contract in external_contracts]
    return {
        "provider": "akshare",
        "mode": "实盘公开数据",
        "strict": True,
        "headline": "实盘模式只使用公开可复核数据；抓取失败就留空，不补值、不猜值、不把缺失当结论。",
        "policy": (
            "国内日线、小时线、库存、外盘库存和外盘价格全部通过 AkShare 公开接口抓取。"
            "程序只根据拿到的原始字段计算 20 日涨跌、30 日库存变化、20 日持仓变化和小时结构。"
        ),
        "items": [
            {
                "title": "国内日线 / 成交量 / 持仓量",
                "detail": "AkShare `futures_zh_daily_sina`；用于连续合约日线收盘、成交量和持仓量。",
            },
            {
                "title": "国内库存",
                "detail": "AkShare `futures_inventory_em` 优先；若接口为空，再回退 `futures_inventory_99`。",
            },
            {
                "title": "小时线",
                "detail": "AkShare `futures_zh_minute_sina(period=\"60\")`；只用于 N 字反转和震荡中枢突破识别。",
            },
            {
                "title": "COMEX 库存",
                "detail": "AkShare `futures_comex_inventory`；当前用于沪金、沪银的海外库存比对。",
            },
            {
                "title": "LME 库存",
                "detail": "AkShare `macro_euro_lme_stock`；当前用于沪铜、沪铝、沪锌的海外库存比对。",
            },
            {
                "title": "外盘价格",
                "detail": "AkShare `futures_foreign_hist`；用于 COMEX / LME 映射品种 20 日价格对比。",
            },
            {
                "title": "国内商品期权",
                "detail": "AkShare `option_hist_shfe`、`option_hist_dce`、`option_hist_czce`；用于 Call/Put 持仓墙、净增仓、隐波和 Skew。海外期权结构需单独接入授权数据源。",
            },
        ],
        "external_contracts": external_labels,
    }


def _normalize_daily(df: pd.DataFrame) -> pd.DataFrame:
    renamed = df.rename(
        columns={
            "日期": "date",
            "时间": "date",
            "datetime": "date",
            "开盘价": "open",
            "最高价": "high",
            "最低价": "low",
            "收盘价": "close",
            "成交量": "volume",
            "持仓量": "open_interest",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "hold": "open_interest",
        }
    )
    required = ["date", "open", "high", "low", "close", "volume"]
    missing = [col for col in required if col not in renamed.columns]
    if missing:
        raise ValueError(f"Daily data missing columns: {missing}")
    if "open_interest" not in renamed.columns:
        renamed["open_interest"] = np.nan
    out = renamed[["date", "open", "high", "low", "close", "volume", "open_interest"]].copy()
    out["date"] = pd.to_datetime(out["date"])
    for col in ["open", "high", "low", "close", "volume", "open_interest"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=["date", "close"]).sort_values("date")


def _normalize_inventory(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["date", "inventory"])
    renamed = df.rename(
        columns={
            "日期": "date",
            "库存": "inventory",
            "库存量": "inventory",
            "期货库存": "inventory",
            "date": "date",
            "inventory": "inventory",
        }
    )
    if "date" not in renamed.columns:
        renamed = renamed.rename(columns={renamed.columns[0]: "date"})
    if "inventory" not in renamed.columns:
        numeric_cols = [col for col in renamed.columns if col != "date"]
        if not numeric_cols:
            return pd.DataFrame(columns=["date", "inventory"])
        renamed = renamed.rename(columns={numeric_cols[-1]: "inventory"})
    out = renamed[["date", "inventory"]].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["inventory"] = pd.to_numeric(out["inventory"], errors="coerce")
    return out.dropna().sort_values("date")


def _normalize_external_inventory(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["date", "inventory"])
    inventory_cols = [col for col in df.columns if "库存量" in str(col) or str(col).endswith("-库存")]
    if "日期" not in df.columns or not inventory_cols:
        return pd.DataFrame(columns=["date", "inventory"])
    out = df[["日期", inventory_cols[0]]].copy()
    out.columns = ["date", "inventory"]
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["inventory"] = pd.to_numeric(out["inventory"], errors="coerce")
    return out.dropna().sort_values("date")


def _extract_lme_inventory(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    column = f"{symbol}-库存"
    if column not in df.columns:
        return pd.DataFrame(columns=["date", "inventory"])
    out = df[["日期", column]].copy()
    out.columns = ["date", "inventory"]
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["inventory"] = pd.to_numeric(out["inventory"], errors="coerce")
    return out.dropna().sort_values("date")


def _inventory_candidates(contract: Contract) -> list[str]:
    candidates: list[str] = []
    if contract.inventory_code:
        candidates.extend([contract.inventory_code, contract.inventory_code.lower()])
    if contract.name:
        candidates.append(contract.name)
    if contract.inventory_symbol:
        candidates.append(contract.inventory_symbol)
    seen: set[str] = set()
    unique: list[str] = []
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique
