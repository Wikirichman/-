import unittest

import pandas as pd

from futures_monitor.config import Contract
from futures_monitor.options import OptionSource, analyze_option_table, normalize_option_table


class OptionStructureTest(unittest.TestCase):
    def test_analyze_option_table_finds_walls_iv_and_skew(self):
        raw = pd.DataFrame(
            {
                "合约代码": ["SR609C5000", "SR609C5100", "SR609P4900", "SR609P4800"],
                "持仓量": [1200, 1800, 2200, 900],
                "增减量": [100, 300, 50, 20],
                "成交量(手)": [30, 40, 25, 10],
                "DELTA": [0.49, 0.24, -0.51, -0.25],
                "隐含波动率": [20.0, 18.0, 22.0, 24.0],
            }
        )
        table = normalize_option_table(raw)
        result = analyze_option_table(
            Contract(symbol="SR0", name="白糖", sector="农产品", inventory_code="SR"),
            table,
            OptionSource("CZCE", "白糖期权", "option_hist_czce"),
            "20260803",
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["call_wall"], 5100.0)
        self.assertEqual(result["put_wall"], 4900.0)
        self.assertAlmostEqual(result["atm_iv"], 0.21)
        self.assertAlmostEqual(result["skew_25d"], 0.06)
        self.assertIn("Call Wall", result["summary"])


if __name__ == "__main__":
    unittest.main()
