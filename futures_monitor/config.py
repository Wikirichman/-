from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Contract:
    symbol: str
    name: str
    sector: str
    inventory_symbol: str | None = None
    inventory_code: str | None = None
    external_market: str | None = None
    external_inventory_symbol: str | None = None
    external_price_symbol: str | None = None


@dataclass(frozen=True)
class MonitorConfig:
    top_n: int
    lookback_days: int
    min_liquidity_score: float
    weights: dict[str, float]
    supply_demand: dict[str, float | int | bool]
    positioning: dict[str, float | int | bool]
    intraday: dict[str, float | int | bool]
    contracts: list[Contract]


def load_config(path: str | Path) -> MonitorConfig:
    config_path = Path(path)
    text = config_path.read_text(encoding="utf-8")
    if config_path.suffix == ".json":
        raw = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError("YAML config requires PyYAML. Use config/universe.json instead.") from exc
        raw = yaml.safe_load(text)
    contracts = [Contract(**item) for item in raw["contracts"]]
    return MonitorConfig(
        top_n=int(raw.get("top_n", 3)),
        lookback_days=int(raw.get("lookback_days", 120)),
        min_liquidity_score=float(raw.get("min_liquidity_score", 0.0)),
        weights={k: float(v) for k, v in raw.get("weights", {}).items()},
        supply_demand=raw.get("supply_demand", {}),
        positioning=raw.get("positioning", {}),
        intraday=raw.get("intraday", {}),
        contracts=contracts,
    )


def config_to_rows(config: MonitorConfig) -> list[dict[str, Any]]:
    return [contract.__dict__ for contract in config.contracts]
