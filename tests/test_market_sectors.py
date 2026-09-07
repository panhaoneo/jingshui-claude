import unittest

from jingshui.market import MarketState, judge_index, judge_market
from jingshui.sectors import leading_sectors, score_sectors
from tests.helpers import make_bars, trend_bars


def choppy_topping_bars(n=300):
    """先涨后跌, 且下跌段放量 —— 制造派发日与破位。"""
    closes, vols = [], []
    price, vol = 10.0, 1000.0
    for i in range(n):
        if i < n - 40:
            price *= 1.004
            vol *= 0.999
        else:
            price *= 0.99
            vol *= 1.03
        closes.append(price)
        vols.append(vol)
    return make_bars(closes, vols)


class TestMarketGate(unittest.TestCase):
    def test_offense_on_clean_uptrend(self):
        v = judge_index("000300.SH", "沪深300", trend_bars(300))
        self.assertIs(v.state, MarketState.OFFENSE)
        self.assertEqual(v.distribution_days, 0)

    def test_defense_on_topping_market(self):
        v = judge_index("000300.SH", "沪深300", choppy_topping_bars())
        self.assertIs(v.state, MarketState.DEFENSE)

    def test_caution_when_history_too_short(self):
        v = judge_index("000300.SH", "沪深300", trend_bars(120))
        self.assertIs(v.state, MarketState.CAUTION)
        self.assertTrue(any("均线数据不足" in r for r in v.reasons))

    def test_market_takes_most_conservative_index(self):
        verdict = judge_market(
            {"000300.SH": trend_bars(300), "399006.SZ": choppy_topping_bars()}
        )
        self.assertIs(verdict.state, MarketState.DEFENSE)
        self.assertEqual(verdict.max_exposure, 0.2)
        self.assertEqual(verdict.stop_loss_pct, 0.03)
        self.assertFalse(verdict.can_open_new)

    def test_offense_allows_full_exposure(self):
        verdict = judge_market({"000300.SH": trend_bars(300)})
        self.assertEqual(verdict.max_exposure, 1.0)
        self.assertEqual(verdict.stop_loss_pct, 0.08)
        self.assertTrue(verdict.can_open_new)

    def test_empty_input_rejected(self):
        with self.assertRaises(ValueError):
            judge_market({})


class TestSectorScoring(unittest.TestCase):
    def setUp(self):
        strong = trend_bars(300, daily=0.006)
        weak_closes = [10.0 * (1.006 ** i) for i in range(200)]
        weak_closes += [weak_closes[-1] * (0.996 ** i) for i in range(1, 101)]
        weak = make_bars(weak_closes)
        flat = make_bars([10.0] * 300)
        self.bars = {"A.TI": strong, "B.TI": weak, "C.TI": flat}
        self.names = {"A.TI": "强势板块", "B.TI": "见顶板块", "C.TI": "横盘板块"}

    def test_strong_sector_ranks_first(self):
        scores = score_sectors(self.bars, self.names)
        self.assertEqual(scores[0].thscode, "A.TI")
        self.assertGreater(scores[0].score, scores[-1].score)

    def test_new_high_requirement_filters_pool(self):
        scores = score_sectors(self.bars, self.names)
        leaders = leading_sectors(scores, top_n=3, require_new_high=True)
        self.assertEqual([s.thscode for s in leaders], ["A.TI"])

    def test_without_new_high_requirement_returns_top_n(self):
        scores = score_sectors(self.bars, self.names)
        leaders = leading_sectors(scores, top_n=3, require_new_high=False)
        self.assertEqual(len(leaders), 3)

    def test_score_bounded(self):
        for s in score_sectors(self.bars, self.names):
            self.assertGreaterEqual(s.score, 0.0)
            self.assertLessEqual(s.score, 100.0)


if __name__ == "__main__":
    unittest.main()
