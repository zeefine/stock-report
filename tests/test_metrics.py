import unittest

from stock_report.metrics import pct_change, rsi, sma, technical_snapshot, volume_ratio


class MetricsTests(unittest.TestCase):
    def test_pct_change(self):
        self.assertAlmostEqual(pct_change(110, 100), 10)
        self.assertIsNone(pct_change(10, 0))

    def test_moving_metrics_require_enough_history(self):
        self.assertIsNone(sma([1, 2], 3))
        self.assertIsNone(rsi(list(range(14)), 14))
        self.assertIsNone(volume_ratio([1] * 20, 20))

    def test_volume_ratio_uses_prior_twenty_sessions(self):
        self.assertEqual(volume_ratio([10] * 20 + [20], 20), 2)

    def test_snapshot_computes_ma200(self):
        rows = [{"date": f"d{i}", "close": float(i), "volume": 10} for i in range(1, 202)]
        result = technical_snapshot(rows)
        self.assertEqual(result["close"], 201)
        self.assertAlmostEqual(result["ma200"], 101.5)
        self.assertEqual(result["volume_ratio20"], 1)


if __name__ == "__main__":
    unittest.main()
