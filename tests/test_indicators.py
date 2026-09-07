import unittest

from jingshui.indicators import (
    distance_from_high,
    distribution_days,
    percentile_rank,
    pullback_depth,
    sma,
    to_bars,
    trailing_return,
    uptrend,
    volume_ratio,
    broke_platform_high,
    broke_ma_on_volume,
)
from tests.helpers import make_bars, trend_bars


class TestBasics(unittest.TestCase):
    def test_sma_needs_enough_data(self):
        self.assertIsNone(sma([1, 2], 5))
        self.assertEqual(sma([1, 2, 3, 4], 2), 3.5)

    def test_to_bars_sorts_ascending(self):
        rows = [
            {"date_ms": 3, "open_price": 1, "high_price": 1, "low_price": 1, "close_price": 1, "volume": 1},
            {"date_ms": 1, "open_price": 2, "high_price": 2, "low_price": 2, "close_price": 2, "volume": 1},
        ]
        bars = to_bars(rows)
        self.assertEqual([b.date_ms for b in bars], [1, 3])

    def test_trailing_return(self):
        bars = make_bars([10.0, 11.0, 12.0])
        self.assertAlmostEqual(trailing_return(bars, 2), 0.2)
        self.assertIsNone(trailing_return(bars, 10))


class TestNewHigh(unittest.TestCase):
    def test_distance_from_high_zero_at_peak(self):
        bars = trend_bars(300)
        d = distance_from_high(bars, 250)
        # high 是 close*1.01, 所以在持续新高时距离恰好是 1%
        self.assertLess(d, 0.011)

    def test_distance_from_high_after_drawdown(self):
        bars = make_bars([10.0] * 100 + [20.0] + [15.0] * 10)
        d = distance_from_high(bars, 250)
        self.assertAlmostEqual(d, (20.2 - 15.0) / 20.2, places=6)


class TestVolume(unittest.TestCase):
    def test_volume_ratio_excludes_today_from_base(self):
        bars = make_bars([10.0] * 21, [100.0] * 20 + [200.0])
        self.assertAlmostEqual(volume_ratio(bars, 20), 2.0)

    def test_volume_ratio_insufficient(self):
        self.assertIsNone(volume_ratio(make_bars([1.0] * 5), 20))


class TestTrend(unittest.TestCase):
    def test_uptrend_true_on_rising_series(self):
        self.assertTrue(uptrend(trend_bars(300)))

    def test_uptrend_none_without_enough_history(self):
        self.assertIsNone(uptrend(trend_bars(100)))

    def test_pullback_depth(self):
        bars = make_bars([10.0] * 50 + [12.0] + [10.8] * 5)
        d = pullback_depth(bars, 60)
        self.assertAlmostEqual(d, (12.12 - 10.8) / 12.12, places=6)

    def test_broke_platform_high(self):
        bars = make_bars([10.0] * 20 + [11.0])
        self.assertTrue(broke_platform_high(bars, 20))
        flat = make_bars([10.0] * 21)
        self.assertFalse(broke_platform_high(flat, 20))


class TestDistributionDays(unittest.TestCase):
    def test_counts_down_days_with_rising_volume(self):
        closes, vols = [], []
        for i in range(40):
            if i >= 15 and i % 3 == 0:
                closes.append(closes[-1] * 0.99)   # 下跌 1%
                vols.append(vols[-1] * 1.5)        # 放量
            else:
                closes.append(100.0 if not closes else closes[-1] * 1.001)
                vols.append(1000.0 if not vols else vols[-1] * 0.9)
        bars = make_bars(closes, vols)
        count = distribution_days(bars, window=25)
        self.assertIsNotNone(count)
        self.assertGreaterEqual(count, 5)

    def test_no_distribution_on_quiet_uptrend(self):
        bars = trend_bars(60)
        self.assertEqual(distribution_days(bars, window=25), 0)

    def test_none_when_insufficient(self):
        self.assertIsNone(distribution_days(trend_bars(10), window=25))


class TestBreakdown(unittest.TestCase):
    def test_broke_ma_on_volume_detects_unrecovered_break(self):
        closes = [10.0 + i * 0.05 for i in range(80)] + [8.0] * 4
        vols = [1000.0] * 80 + [5000.0] * 4
        bars = make_bars(closes, vols)
        self.assertTrue(broke_ma_on_volume(bars, ma_window=50, grace=5))

    def test_no_break_while_above_ma(self):
        bars = trend_bars(100)
        self.assertFalse(broke_ma_on_volume(bars, ma_window=50, grace=5))


class TestPercentile(unittest.TestCase):
    def test_percentile_rank(self):
        pop = [0.1, 0.2, 0.3, 0.4]
        self.assertAlmostEqual(percentile_rank(0.4, pop), 87.5)
        self.assertAlmostEqual(percentile_rank(0.1, pop), 12.5)

    def test_empty_population(self):
        self.assertIsNone(percentile_rank(1.0, []))


if __name__ == "__main__":
    unittest.main()
