from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config
from .providers import fetch_universe, provider_from_name
from .scoring import selections_to_frame, select_leaders


def main() -> None:
    parser = argparse.ArgumentParser(description="Select leading domestic futures contracts.")
    parser.add_argument("--config", default="config/universe.json", help="Path to universe config.")
    parser.add_argument(
        "--provider",
        choices=["sample", "akshare"],
        default="sample",
        help="Data provider. Use sample offline, akshare for live data.",
    )
    parser.add_argument("--output", default="", help="Optional CSV output path.")
    args = parser.parse_args()

    config = load_config(args.config)
    provider = provider_from_name(args.provider)
    bars, inventories, hourly, external_inventories, external_bars = fetch_universe(
        provider,
        config.contracts,
        config.lookback_days,
        int(config.intraday.get("lookback_bars", 80)),
    )
    selections = select_leaders(config, bars, inventories, hourly, external_inventories, external_bars)

    if not selections:
        print("No selections. Check data availability and lookback length.")
        return

    frame = selections_to_frame(selections)
    printable = frame[
        [
            "rank",
            "symbol",
            "name",
            "sector",
            "direction",
            "score",
            "long_score",
            "short_score",
            "return_20d",
            "return_60d",
            "inventory_30d",
            "external_inventory_30d",
            "external_return_20d",
            "oi_20d",
            "volume_ratio_10d",
            "external_market",
            "supply_demand_analysis",
            "hourly_direction",
            "hourly_setup",
            "hourly_trigger_level",
            "reason",
        ]
    ].copy()
    for col in [
        "return_20d",
        "return_60d",
        "inventory_30d",
        "external_inventory_30d",
        "external_return_20d",
        "oi_20d",
        "volume_ratio_10d",
    ]:
        printable[col] = printable[col].map(lambda x: "" if x is None or x != x else f"{x:.2%}")
    print(printable.to_string(index=False))

    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(output, index=False, encoding="utf-8-sig")
        print(f"\nSaved: {output}")


if __name__ == "__main__":
    main()
