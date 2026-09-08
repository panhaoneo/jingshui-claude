"""CLI 参数解析。doctor 的提示信息里会给出可直接复制的命令,
所以那些命令必须真的能被解析, 否则提示就是错的。"""

import io
import unittest
from unittest import mock

from jingshui.cli import build_parser, main


class TestParser(unittest.TestCase):
    def parse(self, argv):
        return build_parser().parse_args(argv)

    def test_option_after_subcommand(self):
        args = self.parse(["doctor", "--pause", "3"])
        self.assertEqual(args.command, "doctor")
        self.assertEqual(args.pause, 3.0)

    def test_option_before_subcommand(self):
        args = self.parse(["--pause", "3", "doctor"])
        self.assertEqual(args.pause, 3.0)

    def test_scan_accepts_throttle_options_after_subcommand(self):
        args = self.parse(
            ["scan", "--request-pause", "1.0", "--org-flow-days", "10", "--out", "o"]
        )
        self.assertEqual(args.request_pause, 1.0)
        self.assertEqual(args.org_flow_days, 10)
        self.assertEqual(args.out, "o")

    def test_hint_commands_from_doctor_are_parseable(self):
        # doctor 打印的两条建议命令必须能被解析
        for argv in (["doctor", "--pause", "3"], ["scan", "--request-pause", "1.0"]):
            with self.subTest(argv=argv):
                self.assertIsNotNone(self.parse(argv).command)

    def test_defaults(self):
        args = self.parse(["scan"])
        self.assertEqual(args.request_pause, 0.15)
        self.assertEqual(args.org_flow_days, 60)
        self.assertEqual(args.top, 5)
        self.assertIsNone(args.rs_reference)

    def test_subcommand_required(self):
        # argparse 会把用法写到 stderr, 屏蔽掉以免污染测试输出
        with mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            self.parse([])

    def test_all_subcommands_registered(self):
        for name in ("market", "sectors", "scan", "explain", "doctor"):
            with self.subTest(name=name):
                self.assertTrue(callable(self.parse([name]).func))


class TestExplain(unittest.TestCase):
    def test_explain_runs_without_api_key(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = main(["explain"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("L0 大盘闸门", text)
        self.assertIn("不构成投资建议", text)

    def test_missing_key_exits_with_code_2(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("os.path.exists", return_value=False), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as errout:
            rc = main(["market"])
        self.assertEqual(rc, 2)
        self.assertIn("HITHINK_FINANCE_API_KEY", errout.getvalue())


if __name__ == "__main__":
    unittest.main()


class TestProviderFlag(unittest.TestCase):
    def parse(self, argv):
        return build_parser().parse_args(argv)

    def test_default_is_hithink(self):
        self.assertEqual(self.parse(["scan"]).provider, "hithink")

    def test_free_provider_selectable_either_order(self):
        self.assertEqual(self.parse(["scan", "--provider", "free"]).provider, "free")
        self.assertEqual(self.parse(["--provider", "free", "scan"]).provider, "free")

    def test_invalid_provider_rejected(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            self.parse(["scan", "--provider", "nope"])

    def test_doctor_accepts_provider(self):
        self.assertEqual(self.parse(["doctor", "--provider", "free"]).provider, "free")


class TestDoctorOnFreeProvider(unittest.TestCase):
    """free 数据源不需要 API Key, 且要如实说明缺失的能力。"""

    def test_runs_without_api_key_and_reports_unsupported(self):
        import os

        from jingshui.cli import cmd_doctor
        from jingshui.providers.free import FreeProvider
        from tests.test_free_provider import FakeOpener, SINA_RAW, tencent_payload

        script = {
            "fqkline": tencent_payload([["2026-01-05", "10", "11", "11.5", "9.5", "1"]]),
            "CompanyFinanceService": SINA_RAW,
            "clist": {"data": {"diff": [{"f12": "BK0475", "f13": 1, "f14": "半导体"}]}},
            "push2his": {"data": {"klines": ["2026-01-05,10,11,11.5,9.5,1000,123456,5.0"]}},
        }
        provider = FreeProvider(opener=FakeOpener(script))
        provider.throttle.scale = 0.0

        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            cmd_doctor(None, client=provider)
        text = out.getvalue()
        self.assertIn("龙虎榜", text)
        self.assertIn("没有机构席位净额", text)
        self.assertNotIn("[FAIL]", text)


class TestDoctorCounting(unittest.TestCase):
    """不支持的能力既不算可用也不算失败, 否则汇总会骗人。"""

    def _free_doctor(self, script):
        from jingshui.cli import cmd_doctor
        from jingshui.providers.free import FreeProvider
        from tests.test_free_provider import FakeOpener

        provider = FreeProvider(opener=FakeOpener(script))
        provider.throttle.scale = 0.0
        with mock.patch("time.sleep"), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cmd_doctor(None, client=provider)
        return rc, out.getvalue()

    def test_unsupported_excluded_from_totals(self):
        from tests.test_free_provider import SINA_RAW, tencent_payload

        script = {
            "fqkline": tencent_payload([["2026-01-05", "10", "11", "11.5", "9.5", "1"]]),
            "CompanyFinanceService": SINA_RAW,
            "clist": {"data": {"diff": [{"f12": "BK0475", "f13": 1, "f14": "半导体"}]}},
            "push2his": {"data": {"klines": ["2026-01-05,10,11,11.5,9.5,1000,123456,5.0"]}},
        }
        rc, text = self._free_doctor(script)
        self.assertEqual(rc, 0)
        self.assertIn("该数据源不支持, 未计入", text)
        # 总数不能把不支持的那一项算进可用端点
        self.assertNotIn("全部 8 个端点可用", text)

    def test_all_networked_probes_failing_reads_as_blocked(self):
        rc, text = self._free_doctor({})  # 任何请求都会 AssertionError -> 归为其他失败
        self.assertEqual(rc, 1)
        self.assertIn("该数据源不支持, 未计入", text)
