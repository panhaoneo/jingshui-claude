"""用假客户端跑通 L0->L3 全流程, 验证编排层与落盘。不发任何网络请求。"""

import json
import os
import shutil
import tempfile
import unittest

from jingshui.market import MarketState
from jingshui.pipeline import Cache, Pipeline, ScanConfig, date_str, days_ago_ms
from tests.helpers import annual_income, balance_row, make_bars, trend_bars
from tests.test_fundamentals import quarters


def bars_to_rows(bars):
    return [
        {
            "date_ms": b.date_ms,
            "open_price": b.open,
            "high_price": b.high,
            "low_price": b.low,
            "close_price": b.close,
            "volume": b.volume,
            "turnover": b.turnover,
        }
        for b in bars
    ]


STRONG = bars_to_rows(trend_bars(320, start=10.0, daily=0.005, volume=2e7))
FLAT = bars_to_rows(make_bars([10.0] * 320, [2e7] * 320))
WEAK = bars_to_rows(trend_bars(320, start=10.0, daily=0.0002, volume=2e7))

# 一只领军股 + 四只滞后股, 这样 RS 百分位的样本总体才有意义
PRICE_BY_CODE = {
    "600000.SH": STRONG,
    "600001.SH": WEAK,
    "600002.SH": WEAK,
    "600003.SH": WEAK,
    "600004.SH": WEAK,
}


class FakeClient:
    """按框架需要的最小接口伪造上游, 记录调用次数以验证缓存生效。"""

    def __init__(self, sector_bars=None):
        self.calls = {}
        self.sector_bars = sector_bars or {"886001.TI": STRONG, "886002.TI": FLAT}

    def _count(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    def index_history(self, thscode, start, end, interval="1d"):
        self._count("index_history")
        return self.sector_bars.get(thscode, STRONG)

    def index_catalog(self, tag="industry"):
        self._count("index_catalog")
        return [
            {"thscode": "886001.TI", "name": "强势行业"},
            {"thscode": "886002.TI", "name": "横盘行业"},
        ]

    def index_constituents(self, thscode):
        self._count("index_constituents")
        return [
            {"thscode": "600000.SH", "ticker": "600000", "name": "领军股"},
            {"thscode": "600001.SH", "ticker": "600001", "name": "跟随股"},
            {"thscode": "600002.SH", "ticker": "600002", "name": "滞后股一"},
            {"thscode": "600003.SH", "ticker": "600003", "name": "滞后股二"},
            {"thscode": "600004.SH", "ticker": "600004", "name": "滞后股三"},
        ]

    def price_history(self, thscode, start, end, adjust="forward", interval="1d"):
        self._count("price_history")
        return PRICE_BY_CODE.get(thscode, FLAT)

    def income_statements(self, thscode, period="quarterly", limit=12):
        self._count(f"income_{period}")
        if period == "annual":
            return [annual_income(y, p) for y, p in
                    [(2020, 100.0), (2021, 140.0), (2022, 190.0), (2023, 260.0)]]
        return quarters({2023: [10.0] * 4, 2024: [16.0, 20.0, 26.0, 34.0]})

    def balance_sheets(self, thscode, period="annual", limit=5):
        self._count("balance")
        return [balance_row(y, e) for y, e in
                [(2020, 500.0), (2021, 600.0), (2022, 750.0), (2023, 900.0)]]

    def dragon_tiger(self, board_type="org", date=None):
        self._count("dragon_tiger")
        return {"stock_items": [{"thscode": "600000.SH", "org_net_value": 5e7}]}


class TestCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_roundtrip(self):
        c = Cache(self.dir)
        c.put("k", {"a": 1})
        self.assertEqual(c.get("k"), {"a": 1})

    def test_miss_returns_none(self):
        self.assertIsNone(Cache(self.dir).get("nope"))

    def test_expired_entry_is_a_miss(self):
        c = Cache(self.dir, ttl_seconds=0)
        c.put("k", {"a": 1})
        # ttl=0 时 falsy, 走的是"不过期"分支, 所以这里用负 ttl 验证过期逻辑
        expired = Cache(self.dir, ttl_seconds=-1)
        self.assertIsNone(expired.get("k"))

    def test_key_with_slash_is_sanitised(self):
        c = Cache(self.dir)
        c.put("a/b:c", [1])
        self.assertEqual(c.get("a/b:c"), [1])


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.client = FakeClient()
        self.config = ScanConfig(
            cache_dir=os.path.join(self.dir, "cache"),
            request_pause=0.0,
            top_sectors=3,
        )
        self.pipe = Pipeline(self.client, self.config)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_market_gate_reads_three_indexes(self):
        verdict = self.pipe.market_gate()
        self.assertIs(verdict.state, MarketState.OFFENSE)
        self.assertEqual(len(verdict.per_index), 3)
        self.assertEqual(self.client.calls["index_history"], 3)

    def test_second_call_hits_cache(self):
        self.pipe.market_gate()
        self.pipe.market_gate()
        self.assertEqual(self.client.calls["index_history"], 3)

    def test_sector_scan_ranks_strong_first(self):
        scores = self.pipe.sector_scan()
        self.assertEqual(scores[0].thscode, "886001.TI")
        leaders = self.pipe.leading(scores)
        self.assertEqual([s.thscode for s in leaders], ["886001.TI"])

    def test_org_flows_aggregates_by_code(self):
        flows = self.pipe.org_flows(days=3)
        self.assertAlmostEqual(flows["600000.SH"], 1.5e8)

    def test_full_run_writes_files_and_summary(self):
        out = os.path.join(self.dir, "output")
        summary = self.pipe.run(out_dir=out)

        self.assertEqual(summary["market"]["state"], "OFFENSE")
        self.assertEqual(summary["leading_sectors"][0]["thscode"], "886001.TI")
        self.assertGreaterEqual(summary["candidate_count"], 1)

        for name in ("summary.json", "sectors.json", "scores.json", "signals.json"):
            path = os.path.join(out, name)
            self.assertTrue(os.path.exists(path), f"{name} 未落盘")
            with open(path, encoding="utf-8") as fh:
                json.load(fh)

        with open(os.path.join(out, "scores.json"), encoding="utf-8") as fh:
            scores = json.load(fh)
        self.assertEqual(scores[0]["sector"], "强势行业")
        self.assertIn("C", scores[0]["factors"])
        self.assertTrue(scores[0]["factors"]["C"]["reasons"])

    def test_defense_market_short_circuits_scan(self):
        topping = bars_to_rows(
            make_bars(
                [10.0 * (1.004 ** i) for i in range(260)]
                + [10.0 * (1.004 ** 259) * (0.99 ** i) for i in range(1, 41)],
                [1000.0] * 260 + [1000.0 * (1.05 ** i) for i in range(1, 41)],
            )
        )
        client = FakeClient()
        client.index_history = lambda code, s, e, interval="1d": topping
        pipe = Pipeline(client, ScanConfig(cache_dir=os.path.join(self.dir, "c2"), request_pause=0.0))
        summary = pipe.run(out_dir=os.path.join(self.dir, "out2"))
        self.assertEqual(summary["market"]["state"], "DEFENSE")
        self.assertIn("note", summary)
        self.assertNotIn("candidate_count", summary)

    def test_no_leading_sector_stops_before_stock_scan(self):
        client = FakeClient(sector_bars={"886001.TI": FLAT, "886002.TI": FLAT})
        client.index_history = lambda code, s, e, interval="1d": (
            STRONG if code.endswith((".SH", ".SZ")) else FLAT
        )
        pipe = Pipeline(client, ScanConfig(cache_dir=os.path.join(self.dir, "c3"), request_pause=0.0))
        summary = pipe.run(out_dir=os.path.join(self.dir, "out3"))
        self.assertEqual(summary["leading_sectors"], [])
        self.assertIn("无主线", summary["note"])


class TestTimeHelpers(unittest.TestCase):
    def test_days_ago(self):
        ref = 1_700_000_000_000
        self.assertEqual(days_ago_ms(1, ref), ref - 86_400_000)

    def test_date_str_format(self):
        self.assertRegex(date_str(1_700_000_000_000), r"^\d{4}-\d{2}-\d{2}$")


if __name__ == "__main__":
    unittest.main()


class TestRequestBudget(unittest.TestCase):
    """限流环境下要能把请求量调下来。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _pipe(self, **cfg):
        client = FakeClient()
        config = ScanConfig(cache_dir=os.path.join(self.dir, "c"), request_pause=0.0, **cfg)
        return Pipeline(client, config), client

    def test_org_flow_days_controls_request_count(self):
        pipe, client = self._pipe(org_flow_days=5)
        pipe.org_flows(pipe.config.org_flow_days)
        self.assertEqual(client.calls["dragon_tiger"], 5)

    def test_run_honours_configured_org_flow_days(self):
        pipe, client = self._pipe(org_flow_days=3)
        pipe.run(out_dir=os.path.join(self.dir, "out"))
        self.assertEqual(client.calls["dragon_tiger"], 3)

    def test_default_org_flow_days_matches_framework_doc(self):
        self.assertEqual(ScanConfig().org_flow_days, 60)
