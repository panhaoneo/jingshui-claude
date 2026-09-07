import unittest

from jingshui.screener import (
    PASS_SCORE,
    StockInput,
    check_exclusions,
    rank_candidates,
    score_a,
    score_c,
    score_i,
    score_l,
    score_n,
    score_stock,
)
from tests.helpers import annual_income, balance_row, make_bars, trend_bars
from tests.test_fundamentals import quarters


def strong_input(**overrides) -> StockInput:
    """一只各项都达标的合成个股。"""
    bars = trend_bars(300, start=10.0, daily=0.005, volume=2e7)
    # 最后一天放量
    bars = bars[:-1] + [type(bars[-1])(
        date_ms=bars[-1].date_ms, open=bars[-1].open, high=bars[-1].high,
        low=bars[-1].low, close=bars[-1].close, volume=bars[-1].volume * 2,
        turnover=bars[-1].close * bars[-1].volume * 2,
    )]
    base = dict(
        thscode="600000.SH",
        name="测试股",
        bars=bars,
        quarterly_income=quarters(
            {2023: [10.0, 10.0, 10.0, 10.0], 2024: [16.0, 20.0, 26.0, 34.0]}
        ),
        annual_income=[annual_income(y, p) for y, p in
                       [(2020, 100.0), (2021, 140.0), (2022, 190.0), (2023, 260.0)]],
        annual_balance=[balance_row(y, e) for y, e in
                        [(2020, 500.0), (2021, 600.0), (2022, 750.0), (2023, 900.0)]],
        market_returns_250d=[0.0, 0.05, 0.1, 0.15, 0.2],
        sector_returns_250d=[0.0, 0.1, 0.2],
        org_net_value_60d=3e8,
    )
    base.update(overrides)
    return StockInput(**base)


class TestExclusions(unittest.TestCase):
    def test_st_excluded(self):
        self.assertIn("ST / *ST 股", check_exclusions(strong_input(is_st=True)))

    def test_new_listing_excluded(self):
        reasons = check_exclusions(strong_input(bars=trend_bars(100, volume=2e7)))
        self.assertTrue(any("上市不足" in r for r in reasons))

    def test_illiquid_excluded(self):
        thin = trend_bars(300, start=1.0, daily=0.005, volume=1000.0)
        reasons = check_exclusions(strong_input(bars=thin))
        self.assertTrue(any("成交额" in r for r in reasons))

    def test_deep_drawdown_excluded(self):
        closes = [10.0 * (1.01 ** i) for i in range(250)]
        closes += [closes[-1] * (0.99 ** i) for i in range(1, 61)]
        weak = make_bars(closes, [2e7] * len(closes))
        reasons = check_exclusions(strong_input(bars=weak))
        self.assertTrue(any("弱者预警线" in r for r in reasons))

    def test_loss_making_excluded(self):
        rows = quarters({2023: [10.0] * 4, 2024: [10.0, 10.0, 10.0, -5.0]})
        reasons = check_exclusions(strong_input(quarterly_income=rows))
        self.assertTrue(any("净利润为负" in r for r in reasons))

    def test_strong_stock_not_excluded(self):
        self.assertEqual(check_exclusions(strong_input()), [])


class TestFactors(unittest.TestCase):
    def test_c_rewards_accelerating_growth(self):
        res = score_c(strong_input())
        self.assertGreater(res.score, 15.0)
        self.assertTrue(any("加速" in r for r in res.reasons))

    def test_c_zeroed_by_two_quarter_deceleration(self):
        rows = quarters({2023: [10.0] * 4, 2024: [30.0, 18.0, 11.0, 10.5]})
        res = score_c(strong_input(quarterly_income=rows))
        self.assertEqual(res.score, 0.0)
        self.assertTrue(any("清零" in r for r in res.reasons))

    def test_a_rewards_three_year_growth_and_roe(self):
        res = score_a(strong_input())
        self.assertGreater(res.score, 12.0)

    def test_a_low_on_flat_earnings(self):
        flat = [annual_income(y, p) for y, p in
                [(2020, 100.0), (2021, 102.0), (2022, 103.0), (2023, 104.0)]]
        res = score_a(strong_input(annual_income=flat))
        self.assertLess(res.score, 8.0)

    def test_n_full_score_at_new_high(self):
        self.assertEqual(score_n(strong_input()).score, 20.0)

    def test_n_zero_when_far_from_high(self):
        closes = [10.0 * (1.01 ** i) for i in range(250)] + [10.0 * (1.01 ** 249) * 0.85] * 20
        bars = make_bars(closes, [2e7] * len(closes))
        self.assertEqual(score_n(strong_input(bars=bars)).score, 0.0)

    def test_l_requires_high_rs(self):
        strong = score_l(strong_input())
        self.assertGreater(strong.score, 15.0)
        laggard = score_l(strong_input(market_returns_250d=[5.0, 6.0, 7.0, 8.0]))
        self.assertEqual(laggard.score, 0.0)

    def test_i_flags_proxy_nature(self):
        res = score_i(strong_input(org_net_value_60d=None))
        self.assertTrue(any("未上榜不等于无机构参与" in r for r in res.reasons))

    def test_i_penalises_net_selling(self):
        buy = score_i(strong_input(org_net_value_60d=3e8)).score
        sell = score_i(strong_input(org_net_value_60d=-3e8)).score
        self.assertGreater(buy, sell)


class TestScoreStock(unittest.TestCase):
    def test_strong_stock_passes(self):
        s = score_stock(strong_input())
        self.assertFalse(s.excluded)
        self.assertGreaterEqual(s.total, PASS_SCORE)
        self.assertTrue(s.passed)

    def test_excluded_stock_has_zero_total(self):
        s = score_stock(strong_input(is_st=True))
        self.assertTrue(s.excluded)
        self.assertEqual(s.total, 0.0)
        self.assertFalse(s.passed)

    def test_required_factor_zero_blocks_pass(self):
        # 把 N 打成 0: 股价远离新高但仍在 30% 以内
        closes = [10.0 * (1.01 ** i) for i in range(250)]
        closes += [closes[-1] * 0.85] * 20
        bars = make_bars(closes, [2e7] * len(closes))
        s = score_stock(strong_input(bars=bars))
        if not s.excluded:
            self.assertEqual(s.factors["N"].score, 0.0)
            self.assertFalse(s.passed)

    def test_metrics_exposed_for_review(self):
        s = score_stock(strong_input())
        for key in ("profit_yoy", "roe", "distance_from_high", "return_250d"):
            self.assertIn(key, s.metrics)

    def test_rank_candidates_sorts_and_filters(self):
        good = score_stock(strong_input())
        bad = score_stock(strong_input(thscode="000002.SZ", is_st=True))
        ranked = rank_candidates([bad, good])
        self.assertEqual([r.thscode for r in ranked], ["600000.SH"])


if __name__ == "__main__":
    unittest.main()
