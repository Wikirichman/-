import unittest

import pandas as pd

from futures_monitor.config import Contract, MonitorConfig
from futures_monitor.intraday import analyze_hourly
from futures_monitor.providers import SampleProvider, fetch_universe
from futures_monitor.scoring import select_leaders


class ScoringTest(unittest.TestCase):
    def test_select_leaders_returns_top_n(self):
        contracts = [
            Contract(
                symbol="CU0",
                name="沪铜",
                sector="有色",
                inventory_symbol="铜",
                inventory_code="CU",
                external_market="LME",
                external_inventory_symbol="铜",
                external_price_symbol="CAD",
            ),
            Contract(symbol="RB0", name="螺纹钢", sector="黑色", inventory_symbol="螺纹钢"),
            Contract(symbol="P0", name="棕榈油", sector="农产品"),
            Contract(
                symbol="AG0",
                name="沪银",
                sector="贵金属",
                inventory_code="AG",
                external_market="COMEX",
                external_inventory_symbol="白银",
                external_price_symbol="SI",
            ),
        ]
        config = MonitorConfig(
            top_n=3,
            lookback_days=120,
            min_liquidity_score=-99,
            weights={"tape": 0.55, "supply_demand": 0.30, "positioning": 0.15},
            supply_demand={"required_for_selection": False},
            positioning={"required_for_selection": False, "min_oi_change_20d": 0.05},
            intraday={"weight": 0.25, "lookback_bars": 80, "required_for_selection": False},
            contracts=contracts,
        )
        bars, inventory, hourly, ext_inventory, ext_bars = fetch_universe(
            SampleProvider(seed=11), contracts, 120, 80
        )
        selections = select_leaders(config, bars, inventory, hourly, ext_inventory, ext_bars)
        self.assertEqual(len(selections), 3)
        self.assertLessEqual({item.direction for item in selections}, {"多头", "空头"})
        self.assertTrue(all(item.reason for item in selections))
        self.assertTrue(all(item.supply_demand_analysis for item in selections))
        for item in selections:
            if item.direction == "多头":
                self.assertGreaterEqual(item.long_score, item.short_score)
            else:
                self.assertGreaterEqual(item.short_score, item.long_score)

    def test_hourly_bullish_n_reversal_breaks_prior_swing_high(self):
        close = [96, 94, 91, 95, 99, 96, 95, 98, 100, 99, 102]
        frame = pd.DataFrame(
            {
                "date": pd.date_range("2026-07-01", periods=len(close), freq="h"),
                "open": close,
                "high": [value + 1 for value in close],
                "low": [value - 1 for value in close],
                "close": close,
                "volume": [100] * 10 + [200],
            }
        )
        signal = analyze_hourly(frame, swing_window=1, range_lookback=5)
        self.assertEqual(signal.direction, "多头")
        self.assertEqual(signal.setup, "N字反转突破摆动高点")
        self.assertGreater(signal.strength, 0)

    def test_hourly_bearish_n_reversal_breaks_prior_swing_low(self):
        close = [104, 106, 109, 105, 101, 104, 105, 102, 100, 101, 98]
        frame = pd.DataFrame(
            {
                "date": pd.date_range("2026-07-01", periods=len(close), freq="h"),
                "open": close,
                "high": [value + 1 for value in close],
                "low": [value - 1 for value in close],
                "close": close,
                "volume": [100] * 10 + [200],
            }
        )
        signal = analyze_hourly(frame, swing_window=1, range_lookback=5)
        self.assertEqual(signal.direction, "空头")
        self.assertEqual(signal.setup, "N字反转跌破摆动低点")
        self.assertGreater(signal.strength, 0)

if __name__ == "__main__":
    unittest.main()
