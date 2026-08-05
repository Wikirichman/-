import unittest

from futures_monitor.web import run_selection


class WebAppTest(unittest.TestCase):
    def test_run_selection_returns_explanation_and_coverage(self):
        payload = run_selection("sample")
        self.assertIn("explanation", payload)
        self.assertIn("sources", payload)
        self.assertIn("coverage", payload)
        self.assertIn("supply_demand_body", payload["explanation"])
        self.assertIn("headline", payload["sources"])
        self.assertIn("items", payload["sources"])
        self.assertIn("inventory_ready", payload["coverage"])
        self.assertIn("external_inventory_ready", payload["coverage"])
        self.assertIn("contracts", payload["coverage"])
        self.assertIn("option_ready", payload["coverage"])
        self.assertIn("option_checked", payload["coverage"])
        if payload["rows"]:
            self.assertIn("supply_demand_analysis", payload["rows"][0])
            self.assertIn("option_structure", payload["rows"][0])


if __name__ == "__main__":
    unittest.main()
