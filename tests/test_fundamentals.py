import unittest

from jingshui import fundamentals as fu
from tests.helpers import annual_income, balance_row, income_row, report_date_ms


def quarters(year_profits: dict[int, list[float]], revenue_mult: float = 5.0):
    """year_profits: {年份: [Q1, Q2, Q3, Q4] 单季净利}, 返回累计口径的报表行。"""
    rows = []
    for year, singles in year_profits.items():
        cum = 0.0
        for q, val in enumerate(singles, start=1):
            cum += val
            rows.append(income_row(year, q, cum, cum * revenue_mult, cum * 0.1))
    return rows


class TestDecumulation(unittest.TestCase):
    def test_single_quarter_recovers_original_values(self):
        rows = quarters({2024: [10.0, 20.0, 15.0, 25.0]})
        series = fu.single_quarter_series(rows)
        self.assertEqual([p.quarter for p in series], [1, 2, 3, 4])
        self.assertEqual([p.net_profit for p in series], [10.0, 20.0, 15.0, 25.0])

    def test_missing_prior_period_yields_none_not_cumulative(self):
        # 只有 Q3 累计, 没有 Q2, 不能拿累计值冒充单季值
        rows = [income_row(2024, 1, 10.0, 50.0), income_row(2024, 3, 45.0, 225.0)]
        series = fu.single_quarter_series(rows)
        q3 = [p for p in series if p.quarter == 3][0]
        self.assertIsNone(q3.net_profit)

    def test_quarter_of(self):
        from tests.helpers import period_end_ms

        self.assertEqual(fu.quarter_of(period_end_ms(2024, 2)), 2)


class TestPointInTime(unittest.TestCase):
    def test_as_of_filter_drops_undisclosed_reports(self):
        rows = quarters({2024: [10.0, 20.0, 15.0, 25.0]})
        # 2024-09-01 时点上, 三季报 (10/31 披露) 和年报都还不可见
        as_of = report_date_ms(2024, 2) + 86_400_000
        series = fu.single_quarter_series(rows, as_of_ms=as_of)
        self.assertEqual([p.quarter for p in series], [1, 2])

    def test_rows_without_report_date_are_dropped(self):
        row = income_row(2024, 1, 10.0, 50.0)
        del row["report_date_ms"]
        self.assertEqual(fu.as_of_filter([row], as_of_ms=10**13), [])

    def test_no_filter_when_as_of_is_none(self):
        rows = quarters({2024: [1.0, 2.0]})
        self.assertEqual(len(fu.as_of_filter(rows, None)), 2)


class TestYoY(unittest.TestCase):
    def setUp(self):
        self.rows = quarters({2023: [10.0, 10.0, 10.0, 10.0], 2024: [15.0, 20.0, 30.0, 40.0]})
        self.series = fu.single_quarter_series(self.rows)

    def test_yoy_uses_same_quarter_last_year(self):
        self.assertAlmostEqual(fu.yoy(self.series, "net_profit"), 3.0)   # 2024Q4: 40 vs 10
        self.assertAlmostEqual(fu.yoy(self.series, "net_profit", back=1), 2.0)  # Q3: 30 vs 10

    def test_accelerating(self):
        self.assertTrue(fu.is_accelerating(self.series, "net_profit"))

    def test_negative_base_is_not_comparable(self):
        rows = quarters({2023: [-5.0, 1.0, 1.0, 1.0], 2024: [10.0, 1.0, 1.0, 1.0]})
        series = fu.single_quarter_series(rows)
        q1_2024 = [p for p in series if p.fiscal_year == 2024 and p.quarter == 1]
        self.assertTrue(q1_2024)
        # 去年同期为负, 增长率无意义
        idx = series.index(q1_2024[0])
        back = len(series) - 1 - idx
        self.assertIsNone(fu.yoy(series, "net_profit", back=back))

    def test_deceleration_detected(self):
        rows = quarters(
            {
                2023: [10.0, 10.0, 10.0, 10.0],
                2024: [30.0, 18.0, 11.0, 10.0],  # 增速 200% -> 80% -> 10% -> 0%
            }
        )
        series = fu.single_quarter_series(rows)
        self.assertTrue(fu.decelerating_two_quarters(series, "net_profit"))

    def test_no_false_deceleration_on_steady_growth(self):
        self.assertFalse(fu.decelerating_two_quarters(self.series, "net_profit"))


class TestAnnual(unittest.TestCase):
    def test_growth_streak_counts_qualifying_years(self):
        rows = [annual_income(y, p) for y, p in
                [(2020, 100.0), (2021, 140.0), (2022, 190.0), (2023, 260.0)]]
        hits, growths = fu.annual_growth_streak(rows, years=3)
        self.assertEqual(hits, 3)
        self.assertEqual(len(growths), 3)

    def test_growth_streak_penalises_flat_years(self):
        rows = [annual_income(y, p) for y, p in
                [(2020, 100.0), (2021, 105.0), (2022, 108.0), (2023, 110.0)]]
        hits, _ = fu.annual_growth_streak(rows, years=3)
        self.assertEqual(hits, 0)

    def test_roe(self):
        inc = [annual_income(2023, 200.0)]
        bal = [balance_row(2023, 1000.0)]
        self.assertAlmostEqual(fu.roe(inc, bal), 0.20)

    def test_roe_none_on_negative_equity(self):
        self.assertIsNone(fu.roe([annual_income(2023, 200.0)], [balance_row(2023, -10.0)]))


class TestMargin(unittest.TestCase):
    def test_net_margin(self):
        series = fu.single_quarter_series(quarters({2024: [10.0]}, revenue_mult=5.0))
        self.assertAlmostEqual(fu.net_margin(series), 0.2)

    def test_margin_near_high(self):
        rows = quarters({2023: [10.0, 10.0, 10.0, 10.0], 2024: [10.0, 10.0, 10.0, 10.0]})
        series = fu.single_quarter_series(rows)
        self.assertTrue(fu.margin_near_high(series))


if __name__ == "__main__":
    unittest.main()
