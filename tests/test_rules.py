import unittest

from jingshui.market import MarketState
from jingshui.rules import (
    Action,
    Position,
    SellAction,
    STOP_LOSS_DEFENSE,
    STOP_LOSS_NORMAL,
    check_portfolio,
    evaluate_entry,
    evaluate_exit,
    next_batch,
    stop_loss_price,
)
from tests.helpers import make_bars, trend_bars
from tests.test_fundamentals import quarters


def pullback_bars(depth: float, rebound_volume: float = 5e6, base_volume: float = 1e6):
    """先上涨到高点, 再回撤 depth, 最后一天放量反弹。"""
    up = [10.0 * (1.01 ** i) for i in range(120)]
    peak = up[-1]
    down = [peak * (1 - depth * (i + 1) / 10) for i in range(10)]
    closes = up + down + [down[-1] * 1.03]
    vols = [base_volume] * (len(closes) - 1) + [rebound_volume]
    return make_bars(closes, vols)


class TestEntry(unittest.TestCase):
    def test_no_new_position_outside_offense(self):
        for state in (MarketState.CAUTION, MarketState.DEFENSE):
            sig = evaluate_entry("600000.SH", pullback_bars(0.15), state, True)
            self.assertIs(sig.action, Action.REJECT)
            self.assertTrue(any(state.value in r for r in sig.reasons))

    def test_buy_on_confirmed_rebound_inside_pullback_band(self):
        sig = evaluate_entry("600000.SH", pullback_bars(0.15), MarketState.OFFENSE, True)
        self.assertIs(sig.action, Action.BUY)
        self.assertIsNotNone(sig.stop_loss_price)
        self.assertTrue(any("放量上攻确认" in r or "突破回调平台高点" in r for r in sig.reasons))

    def test_watch_when_rebound_not_confirmed(self):
        # 回撤在区间内但反弹日没有放量, 也没破平台高点
        bars = pullback_bars(0.15, rebound_volume=1e6)
        bars = bars[:-1]  # 去掉反弹日, 停在回撤末端
        sig = evaluate_entry("600000.SH", bars, MarketState.OFFENSE, True)
        self.assertIs(sig.action, Action.WATCH)

    def test_reject_when_drawdown_too_deep(self):
        sig = evaluate_entry("600000.SH", pullback_bars(0.40), MarketState.OFFENSE, True)
        self.assertIs(sig.action, Action.REJECT)
        self.assertTrue(any("弱者" in r for r in sig.reasons))

    def test_reject_when_below_ma50(self):
        sig = evaluate_entry("600000.SH", pullback_bars(0.15), MarketState.OFFENSE, False)
        self.assertIs(sig.action, Action.REJECT)
        self.assertTrue(any("50 日线" in r for r in sig.reasons))

    def test_watch_when_barely_pulled_back(self):
        bars = trend_bars(200)
        sig = evaluate_entry("600000.SH", bars, MarketState.OFFENSE, True)
        self.assertIn(sig.action, (Action.WATCH, Action.BUY))


class TestBatching(unittest.TestCase):
    def test_first_batch_always_allowed(self):
        ok, _ = next_batch(Position("600000.SH", "半导体"), 10.0, trend_bars(100), True)
        self.assertTrue(ok)

    def test_never_average_down(self):
        pos = Position("600000.SH", "半导体", batches=[(10.0, 0.10)])
        ok, why = next_batch(pos, 9.0, trend_bars(100), True)
        self.assertFalse(ok)
        self.assertIn("禁止向下加仓", why)

    def test_second_batch_needs_gain_threshold(self):
        pos = Position("600000.SH", "半导体", batches=[(10.0, 0.10)])
        ok, _ = next_batch(pos, 10.1, trend_bars(100), True)
        self.assertFalse(ok)
        ok, why = next_batch(pos, 10.3, trend_bars(100), True)
        self.assertTrue(ok)
        self.assertIn("未破 50 日线", why)

    def test_second_batch_blocked_below_ma50(self):
        pos = Position("600000.SH", "半导体", batches=[(10.0, 0.10)])
        ok, why = next_batch(pos, 10.5, trend_bars(100), False)
        self.assertFalse(ok)
        self.assertIn("跌破 50 日线", why)

    def test_third_batch_needs_breakout(self):
        pos = Position("600000.SH", "半导体", batches=[(10.0, 0.10), (10.3, 0.09)])
        flat = make_bars([11.0] * 40, [1e6] * 40)
        ok, why = next_batch(pos, 11.0, flat, True)
        self.assertFalse(ok)
        self.assertIn("突破前高并放量", why)

    def test_no_fourth_batch(self):
        pos = Position("600000.SH", "半导体", batches=[(10.0, 0.1), (10.3, 0.09), (11.0, 0.06)])
        ok, why = next_batch(pos, 12.0, trend_bars(100), True)
        self.assertFalse(ok)
        self.assertIn("三批已满", why)


class TestStopLoss(unittest.TestCase):
    def test_normal_and_defense_levels(self):
        self.assertAlmostEqual(stop_loss_price(100.0, MarketState.OFFENSE), 100 * (1 - STOP_LOSS_NORMAL))
        self.assertAlmostEqual(stop_loss_price(100.0, MarketState.DEFENSE), 100 * (1 - STOP_LOSS_DEFENSE))

    def test_stop_uses_each_batch_price_not_average(self):
        pos = Position("600000.SH", "半导体", batches=[(10.0, 0.1), (12.0, 0.1)])
        bars = make_bars([10.5] * 60, [1e6] * 60)
        sig = evaluate_exit("600000.SH", bars, pos, MarketState.OFFENSE)
        # 均价 11, -8% 是 10.12, 现价 10.5 高于均价止损位;
        # 但第二批 12.0 的止损位 11.04 已被击穿
        self.assertIs(sig.action, SellAction.STOP_OUT)
        self.assertTrue(any("第 2 批" in r for r in sig.reasons))


class TestExit(unittest.TestCase):
    def _pos(self, price=10.0, held=100):
        return Position("600000.SH", "半导体", batches=[(price, 0.2)], held_days=held)

    def test_hold_when_nothing_triggers(self):
        bars = trend_bars(300, start=10.0, daily=0.002)
        sig = evaluate_exit("600000.SH", bars, self._pos(price=8.0), MarketState.OFFENSE)
        self.assertIs(sig.action, SellAction.HOLD)

    def test_fast_gain_locks_position_for_eight_weeks(self):
        closes = [10.0] * 100 + [10.0 * (1.02 ** i) for i in range(1, 16)]
        bars = make_bars(closes, [1e6] * len(closes))
        pos = self._pos(price=9.0, held=10)
        sig = evaluate_exit("600000.SH", bars, pos, MarketState.CAUTION)
        self.assertIs(sig.action, SellAction.HOLD)
        self.assertTrue(any("8 周锁定规则" in r for r in sig.reasons))

    def test_stop_loss_overrides_fast_gain_lock(self):
        closes = [10.0] * 100 + [10.0 * (1.02 ** i) for i in range(1, 16)]
        bars = make_bars(closes, [1e6] * len(closes))
        # 买在最高点之上, 现价已跌破 8%
        pos = Position("600000.SH", "半导体", batches=[(closes[-1] * 1.2, 0.2)], held_days=5)
        sig = evaluate_exit("600000.SH", bars, pos, MarketState.OFFENSE)
        self.assertIs(sig.action, SellAction.STOP_OUT)

    def test_earnings_deceleration_exits(self):
        bars = trend_bars(300, start=10.0, daily=0.002)
        rows = quarters({2023: [10.0] * 4, 2024: [30.0, 18.0, 11.0, 10.5]})
        sig = evaluate_exit(
            "600000.SH", bars, self._pos(price=8.0), MarketState.OFFENSE, quarterly_income=rows
        )
        self.assertIs(sig.action, SellAction.EXIT)
        self.assertTrue(any("连续两季回落" in r for r in sig.reasons))

    def test_rs_dropout_exits(self):
        bars = trend_bars(300, start=10.0, daily=0.0005)
        sig = evaluate_exit(
            "600000.SH", bars, self._pos(price=8.0), MarketState.OFFENSE,
            market_returns_250d=[5.0, 6.0, 7.0, 8.0],
        )
        self.assertIs(sig.action, SellAction.EXIT)
        self.assertTrue(any("RS 百分位" in r for r in sig.reasons))

    def test_defense_trims_half(self):
        bars = trend_bars(300, start=10.0, daily=0.002)
        sig = evaluate_exit("600000.SH", bars, self._pos(price=5.0), MarketState.DEFENSE)
        self.assertIs(sig.action, SellAction.TRIM_HALF)


class TestPortfolio(unittest.TestCase):
    def test_clean_portfolio_passes(self):
        positions = [
            Position("A", "半导体", [(10.0, 0.2)]),
            Position("B", "半导体", [(10.0, 0.15)]),
            Position("C", "创新药", [(10.0, 0.2)]),
            Position("D", "电力设备", [(10.0, 0.2)]),
        ]
        self.assertTrue(check_portfolio(positions, MarketState.OFFENSE).ok)

    def test_single_position_cap(self):
        res = check_portfolio([Position("A", "半导体", [(10.0, 0.30)])], MarketState.OFFENSE)
        self.assertFalse(res.ok)
        self.assertTrue(any("单票仓位" in v for v in res.violations))

    def test_sector_cap(self):
        positions = [
            Position("A", "半导体", [(10.0, 0.25)]),
            Position("B", "半导体", [(10.0, 0.25)]),
        ]
        res = check_portfolio(positions, MarketState.OFFENSE)
        self.assertTrue(any("板块" in v for v in res.violations))

    def test_exposure_cap_by_market_state(self):
        positions = [Position(str(i), f"板块{i}", [(10.0, 0.1)]) for i in range(5)]
        self.assertTrue(check_portfolio(positions, MarketState.OFFENSE).ok)
        res = check_portfolio(positions, MarketState.DEFENSE)
        self.assertTrue(any("总仓位" in v for v in res.violations))

    def test_leverage_forbidden(self):
        res = check_portfolio([Position("A", "半导体", [(10.0, 0.2)])], MarketState.OFFENSE, leverage=1.5)
        self.assertTrue(any("杠杆" in v for v in res.violations))

    def test_too_many_positions(self):
        positions = [Position(str(i), f"板块{i}", [(10.0, 0.05)]) for i in range(8)]
        res = check_portfolio(positions, MarketState.OFFENSE)
        self.assertTrue(any("超过上限" in v for v in res.violations))


if __name__ == "__main__":
    unittest.main()
