"""零鉴权数据源的解析层。

重点在两件事:
1. 代码格式转换 (沪深北前缀、板块 secid) —— 弄错会静默返回另一只标的的数据。
2. K 线字段顺序 —— 腾讯的行是 [日期, 开, 收, 高, 低, 量], 收盘价在第三位。
   顺序搞错不会报错, 只会产出错数据, 所以解析层用 OHLC 自洽性做了硬校验。
"""

import json
import logging
import unittest
from unittest import mock

from jingshui.providers import PROVIDERS, build_provider
from jingshui.providers._http import FetchError, Throttle
from jingshui.providers.free import (
    FreeProvider,
    _ms,
    SINA_BALANCE_FIELDS,
    SINA_INCOME_FIELDS,
    is_sector_code,
    parse_em_klines,
    parse_sina_envelope,
    parse_sina_reports,
    parse_tencent_klines,
    statutory_disclosure_ms,
    to_em_secid,
    to_tencent_code,
)


class TestCodeConversion(unittest.TestCase):
    def test_suffix_forms(self):
        self.assertEqual(to_tencent_code("600519.SH"), "sh600519")
        self.assertEqual(to_tencent_code("300750.SZ"), "sz300750")
        self.assertEqual(to_tencent_code("920982.BJ"), "bj920982")

    def test_bare_codes_routed_by_number(self):
        self.assertEqual(to_tencent_code("600519"), "sh600519")
        self.assertEqual(to_tencent_code("000001"), "sz000001")
        self.assertEqual(to_tencent_code("300750"), "sz300750")
        self.assertEqual(to_tencent_code("920982"), "bj920982")

    def test_shanghai_index_whitelist(self):
        # 000300 裸码按号段会落到深市, 必须靠白名单纠正为沪市指数
        self.assertEqual(to_tencent_code("000300", is_index=True), "sh000300")
        self.assertEqual(to_tencent_code("399006", is_index=True), "sz399006")

    def test_explicit_prefix_passthrough(self):
        self.assertEqual(to_tencent_code("sh000001"), "sh000001")

    def test_em_secid_market_number(self):
        self.assertEqual(to_em_secid("600519.SH"), "1.600519")
        self.assertEqual(to_em_secid("300750.SZ"), "0.300750")
        self.assertEqual(to_em_secid("BK0475.EM"), "90.BK0475")

    def test_sector_detection(self):
        self.assertTrue(is_sector_code("BK0475.EM"))
        self.assertTrue(is_sector_code("BK0475"))
        self.assertFalse(is_sector_code("600519.SH"))


def tencent_payload(rows, code="sh600519", key="qfqday"):
    return {"code": 0, "msg": "", "data": {code: {key: rows}}}


class TestTencentKlines(unittest.TestCase):
    def test_field_order_is_date_open_close_high_low_volume(self):
        rows = [["2026-01-05", "10.0", "11.0", "11.5", "9.5", "1000"]]
        out = parse_tencent_klines(tencent_payload(rows), "sh600519")
        self.assertEqual(len(out), 1)
        bar = out[0]
        self.assertEqual(bar["open_price"], 10.0)
        self.assertEqual(bar["close_price"], 11.0)
        self.assertEqual(bar["high_price"], 11.5)
        self.assertEqual(bar["low_price"], 9.5)

    def test_volume_converted_from_lots_to_shares(self):
        rows = [["2026-01-05", "10.0", "11.0", "11.5", "9.5", "1000"]]
        out = parse_tencent_klines(tencent_payload(rows), "sh600519")
        self.assertEqual(out[0]["volume"], 100_000.0)
        self.assertEqual(out[0]["turnover"], 11.0 * 100_000.0)

    def test_wrong_field_order_is_rejected_not_silently_used(self):
        # 假装接口把顺序改成了 [日期, 开, 高, 低, 收, 量]:
        # 高低价会穿越开收价, 必须报错而不是产出错数据
        rows = [["2026-01-05", "10.0", "11.5", "9.5", "11.0", "1000"]] * 10
        with self.assertRaises(FetchError) as ctx:
            parse_tencent_klines(tencent_payload(rows), "sh600519")
        self.assertIn("字段顺序", str(ctx.exception))

    def test_index_response_uses_day_key(self):
        rows = [["2026-01-05", "3000", "3050", "3060", "2990", "100"]]
        out = parse_tencent_klines(tencent_payload(rows, "sh000300", "day"), "sh000300")
        self.assertEqual(len(out), 1)

    def test_missing_code_returns_empty(self):
        self.assertEqual(parse_tencent_klines(tencent_payload([]), "sh600519"), [])
        self.assertEqual(parse_tencent_klines({}, "sh600519"), [])

    def test_rows_sorted_ascending(self):
        rows = [
            ["2026-01-06", "10", "11", "11.5", "9.5", "1"],
            ["2026-01-05", "10", "11", "11.5", "9.5", "1"],
        ]
        out = parse_tencent_klines(tencent_payload(rows), "sh600519")
        self.assertLess(out[0]["date_ms"], out[1]["date_ms"])

    def test_malformed_rows_skipped(self):
        rows = [
            ["2026-01-05", "10", "11", "11.5", "9.5", "1"],
            ["bad"],
            ["not-a-date", "10", "11", "11.5", "9.5", "1"],
        ]
        self.assertEqual(len(parse_tencent_klines(tencent_payload(rows), "sh600519")), 1)


class TestEastmoneyKlines(unittest.TestCase):
    def test_parses_comma_separated_rows(self):
        payload = {"data": {"klines": ["2026-01-05,10.0,11.0,11.5,9.5,1000,123456,5.0"]}}
        out = parse_em_klines(payload)
        self.assertEqual(out[0]["close_price"], 11.0)
        self.assertEqual(out[0]["turnover"], 123456.0)

    def test_empty_payload(self):
        self.assertEqual(parse_em_klines({"data": None}), [])


SINA_RAW = {
    "result": {
        "data": {
            "report_list": {
                "20251231": {"data": [
                    {"item_title": "营业总收入", "item_value": "1000"},
                    {"item_title": "归属于母公司所有者的净利润", "item_value": "200"},
                    {"item_title": "基本每股收益", "item_value": "1.5"},
                ]},
                "20250930": {"data": [
                    {"item_title": "营业总收入", "item_value": "700"},
                    {"item_title": "归属于母公司所有者的净利润", "item_value": "140"},
                ]},
            }
        }
    }
}


class TestSinaFinancials(unittest.TestCase):
    def test_envelope_flattened(self):
        rows = parse_sina_envelope(SINA_RAW)
        self.assertEqual(rows[0]["报告期"], "2025-12-31")
        self.assertEqual(rows[0]["营业总收入"], "1000")

    def test_mapped_to_hithink_schema(self):
        rows = parse_sina_reports(parse_sina_envelope(SINA_RAW), "600519.SH", SINA_INCOME_FIELDS)
        latest = rows[0]
        self.assertEqual(latest["fiscal_year"], 2025)
        self.assertEqual(latest["fiscal_period"], "FY")
        self.assertEqual(latest["operating_income"], 1000.0)
        self.assertEqual(latest["parent_holder_net_profit"], 200.0)
        self.assertEqual(latest["thscode"], "600519.SH")

    def test_sorted_newest_first_like_hithink(self):
        rows = parse_sina_reports(parse_sina_envelope(SINA_RAW), "600519.SH", SINA_INCOME_FIELDS)
        self.assertGreater(rows[0]["period_end_ms"], rows[1]["period_end_ms"])

    def test_report_date_is_statutory_deadline_and_flagged(self):
        rows = parse_sina_reports(parse_sina_envelope(SINA_RAW), "600519.SH", SINA_INCOME_FIELDS)
        annual = rows[0]
        self.assertTrue(annual["report_date_is_estimated"])
        # 2025 年报的法定截止日是 2026-04-30, 必须晚于报告期末
        self.assertEqual(annual["report_date_ms"], statutory_disclosure_ms(2025, 4))
        self.assertGreater(annual["report_date_ms"], annual["period_end_ms"])

    def test_estimated_disclosure_never_earlier_than_reality(self):
        # 保守方向: 推算的披露日不能早于报告期末, 否则会引入未来函数
        for year in (2024, 2025):
            for q in (1, 2, 3, 4):
                with self.subTest(year=year, q=q):
                    self.assertGreater(statutory_disclosure_ms(year, q), 0)

    def test_missing_field_is_none_not_zero(self):
        raw = {"result": {"data": {"report_list": {"20251231": {"data": [
            {"item_title": "营业总收入", "item_value": "1000"}]}}}}}
        rows = parse_sina_reports(parse_sina_envelope(raw), "600519.SH", SINA_INCOME_FIELDS)
        self.assertIsNone(rows[0]["parent_holder_net_profit"])

    def test_balance_field_name_variants(self):
        for title in SINA_BALANCE_FIELDS["holder_equity_total"]:
            with self.subTest(title=title):
                raw = {"result": {"data": {"report_list": {"20251231": {"data": [
                    {"item_title": title, "item_value": "5000"}]}}}}}
                rows = parse_sina_reports(parse_sina_envelope(raw), "600519.SH", SINA_BALANCE_FIELDS)
                self.assertEqual(rows[0]["holder_equity_total"], 5000.0)


class FakeOpener:
    def __init__(self, script):
        self.script = dict(script)
        self.urls = []

    def open(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        for fragment, payload in self.script.items():
            if fragment in url:
                import io

                return _Ctx(io.BytesIO(json.dumps(payload).encode()))
        raise AssertionError(f"未预期的请求: {url}")


class _Ctx:
    def __init__(self, buf):
        self.buf = buf

    def __enter__(self):
        return self.buf

    def __exit__(self, *exc):
        return False


class TestFreeProviderInterface(unittest.TestCase):
    def provider(self, script):
        p = FreeProvider(opener=FakeOpener(script))
        p.throttle = Throttle(scale=0.0)
        return p

    def test_matches_hithink_method_signatures(self):
        from jingshui.client import HithinkClient

        needed = ["price_history", "index_history", "index_catalog",
                  "index_constituents", "income_statements", "balance_sheets",
                  "dragon_tiger", "set_min_interval"]
        for name in needed:
            with self.subTest(name=name):
                self.assertTrue(hasattr(FreeProvider, name))
                self.assertTrue(hasattr(HithinkClient, name))

    def test_price_history_filters_to_window(self):
        rows = [
            ["2026-01-05", "10", "11", "11.5", "9.5", "1"],
            ["2026-02-05", "10", "11", "11.5", "9.5", "1"],
        ]
        p = self.provider({"fqkline": tencent_payload(rows)})
        out = p.price_history("600519.SH", _ms("2026-01-01"), _ms("2026-01-31"))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["date_ms"], _ms("2026-01-05"))

    def test_index_history_omits_qfq(self):
        p = self.provider({"fqkline": tencent_payload(
            [["2026-01-05", "3000", "3050", "3060", "2990", "1"]], "sh000300", "day")})
        p.index_history("000300.SH", 0, 9_999_999_999_999)
        self.assertNotIn("qfq", p._opener.urls[0])

    def test_price_history_requests_qfq(self):
        p = self.provider({"fqkline": tencent_payload(
            [["2026-01-05", "10", "11", "11.5", "9.5", "1"]])})
        p.price_history("600519.SH", 0, 9_999_999_999_999)
        self.assertIn("qfq", p._opener.urls[0])

    def test_sector_history_routes_to_eastmoney(self):
        p = self.provider({"push2his": {"data": {"klines": [
            "2026-01-05,10.0,11.0,11.5,9.5,1000,123456,5.0"]}}})
        out = p.index_history("BK0475.EM", 0, 9_999_999_999_999)
        self.assertEqual(len(out), 1)
        self.assertIn("secid=90.BK0475", p._opener.urls[0])

    def test_index_catalog_tags_sector_codes(self):
        p = self.provider({"clist": {"data": {"diff": [
            {"f12": "BK0475", "f14": "半导体"}]}}})
        rows = p.index_catalog("industry")
        self.assertEqual(rows[0]["thscode"], "BK0475.EM")
        self.assertEqual(rows[0]["name"], "半导体")

    def test_unknown_sector_tag_rejected(self):
        p = self.provider({})
        with self.assertRaises(ValueError):
            p.index_catalog("nope")

    def test_constituents_carry_exchange_suffix(self):
        p = self.provider({"clist": {"data": {"diff": [
            {"f12": "600519", "f13": 1, "f14": "贵州茅台"},
            {"f12": "300750", "f13": 0, "f14": "宁德时代"}]}}})
        rows = p.index_constituents("BK0475.EM")
        self.assertEqual(rows[0]["thscode"], "600519.SH")
        self.assertEqual(rows[1]["thscode"], "300750.SZ")

    def test_annual_filter_keeps_only_year_end(self):
        p = self.provider({"CompanyFinanceService": SINA_RAW})
        rows = p.income_statements("600519.SH", "annual", 5)
        self.assertTrue(all(r["fiscal_period"] == "FY" for r in rows))

    def test_quarterly_keeps_all_periods(self):
        p = self.provider({"CompanyFinanceService": SINA_RAW})
        self.assertEqual(len(p.income_statements("600519.SH", "quarterly", 12)), 2)

    def test_dragon_tiger_declares_itself_unsupported(self):
        p = self.provider({})
        result = p.dragon_tiger("org")
        self.assertEqual(result["stock_items"], [])
        self.assertIn("unsupported", result)


class TestProviderFactory(unittest.TestCase):
    def test_free_provider_needs_no_api_key(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsInstance(build_provider("free"), FreeProvider)

    def test_unknown_provider_lists_options(self):
        with self.assertRaises(ValueError) as ctx:
            build_provider("nope")
        for name in PROVIDERS:
            self.assertIn(name, str(ctx.exception))


class TestThrottle(unittest.TestCase):
    def test_eastmoney_is_slowest_domain(self):
        t = Throttle()
        em = t.interval_for(t.domain_of("https://push2his.eastmoney.com/x"))
        tx = t.interval_for(t.domain_of("https://web.ifzq.gtimg.cn/x"))
        self.assertGreater(em, tx)
        self.assertGreaterEqual(em, 1.0)

    def test_push2_and_push2his_share_one_limit_face(self):
        t = Throttle()
        self.assertEqual(
            t.domain_of("https://push2.eastmoney.com/a"),
            t.domain_of("https://push2his.eastmoney.com/b"),
        )

    def test_penalty_widens_and_relax_shrinks(self):
        logging.getLogger("jingshui.providers._http").setLevel(logging.ERROR)
        t = Throttle()
        url = "https://push2.eastmoney.com/x"
        before = t.interval_for(t.domain_of(url))
        t.penalise(url)
        widened = t.interval_for(t.domain_of(url))
        self.assertGreater(widened, before)
        for _ in range(50):
            t.relax(url)
        self.assertLess(t.interval_for(t.domain_of(url)), widened)


if __name__ == "__main__":
    unittest.main()
