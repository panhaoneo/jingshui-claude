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
