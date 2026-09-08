import io
import json
import unittest
import urllib.error
from unittest import mock

from jingshui.client import (
    HithinkClient,
    HithinkError,
    HttpStatusError,
    MissingApiKey,
    load_api_key,
)


def http_error(status, reason="Too Many Requests", body="", retry_after=None):
    """构造一个真实形状的 urllib HTTPError。"""
    import email.message

    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(
        "https://fuyao.aicubes.cn/x", status, reason, headers, io.BytesIO(body.encode())
    )


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeOpener:
    """记录请求并按脚本返回响应, 用于离线验证契约处理。"""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(json.dumps(item).encode("utf-8"))


def client(script):
    return HithinkClient(api_key="test-key", opener=FakeOpener(script), max_retries=2)


def ok(data):
    return {"code": 0, "message": "ok", "request_id": "r1", "data": data}


def err(code, message="boom"):
    return {"code": code, "message": message, "request_id": "r1", "data": None}


class TestApiKey(unittest.TestCase):
    def test_env_var_used(self):
        with mock.patch.dict("os.environ", {"HITHINK_FINANCE_API_KEY": " secret "}):
            self.assertEqual(load_api_key(), "secret")

    def test_missing_key_raises_with_guidance(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("os.path.exists", return_value=False):
                with self.assertRaises(MissingApiKey) as ctx:
                    load_api_key()
        self.assertIn("HITHINK_FINANCE_API_KEY", str(ctx.exception))

    def test_key_sent_as_header_not_query(self):
        c = client([ok({"item": []})])
        c.price_snapshot(["600519.SH"])
        req = c._opener.requests[0]
        self.assertEqual(req.get_header("X-api-key"), "test-key")
        self.assertNotIn("test-key", req.full_url)


class TestEnvelope(unittest.TestCase):
    def test_success_returns_data(self):
        c = client([ok({"item": [{"thscode": "600519.SH"}]})])
        self.assertEqual(c.price_snapshot(["600519.SH"])[0]["thscode"], "600519.SH")

    def test_http_200_with_business_error_raises(self):
        c = client([err(3001, "标的不存在")])
        with self.assertRaises(HithinkError) as ctx:
            c.price_snapshot(["999999.SH"])
        self.assertEqual(ctx.exception.code, 3001)
        self.assertEqual(ctx.exception.request_id, "r1")

    def test_null_data_is_not_treated_as_empty(self):
        c = client([{"code": 0, "message": "ok", "request_id": "r1", "data": None}])
        with self.assertRaises(HithinkError):
            c.price_snapshot(["600519.SH"])

    def test_missing_item_key_yields_empty_list(self):
        c = client([ok({"timestamp": 1})])
        self.assertEqual(c.price_snapshot(["600519.SH"]), [])


class TestRetryPolicy(unittest.TestCase):
    def test_caller_fixable_errors_are_not_retried(self):
        for code in (1001, 1002, 1003, 1004, 2001, 2003, 3001, 3004):
            c = client([err(code)])
            with self.assertRaises(HithinkError):
                c.price_snapshot(["600519.SH"])
            self.assertEqual(len(c._opener.requests), 1, f"code={code} 不应重试")

    def test_data_not_ready_is_not_retried(self):
        c = client([err(3002, "数据尚未准备")])
        with self.assertRaises(HithinkError):
            c.price_snapshot(["600519.SH"])
        self.assertEqual(len(c._opener.requests), 1)

    def test_rate_limit_is_retried_then_succeeds(self):
        c = client([err(4001), ok({"item": [{"thscode": "600519.SH"}]})])
        with mock.patch("time.sleep"):
            rows = c.price_snapshot(["600519.SH"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(c._opener.requests), 2)

    def test_server_error_exhausts_retries(self):
        c = client([err(5001), err(5001), err(5001)])
        with mock.patch("time.sleep"):
            with self.assertRaises(HithinkError):
                c.price_snapshot(["600519.SH"])
        self.assertEqual(len(c._opener.requests), 3)

    def test_network_error_is_retried(self):
        c = client([urllib.error.URLError("reset"), ok({"item": []})])
        with mock.patch("time.sleep"):
            self.assertEqual(c.price_snapshot(["600519.SH"]), [])


class TestEndpointParams(unittest.TestCase):
    def test_valuation_batch_limit_enforced_locally(self):
        c = client([ok({"item": []})])
        with self.assertRaises(ValueError):
            c.valuations([f"{i:06d}.SH" for i in range(101)])

    def test_index_history_omits_adjust(self):
        c = client([ok({"item": []})])
        c.index_history("886042.TI", 1, 2)
        self.assertNotIn("adjust", c._opener.requests[0].full_url)

    def test_price_history_defaults_to_forward_adjust(self):
        c = client([ok({"item": []})])
        c.price_history("600519.SH", 1, 2)
        self.assertIn("adjust=forward", c._opener.requests[0].full_url)

    def test_ticker_list_paginates_until_short_page(self):
        page1 = ok({"item": [{"thscode": f"{i}.SH"} for i in range(3)]})
        page2 = ok({"item": [{"thscode": "x.SH"}]})
        c = client([page1, page2])
        rows = list(c.list_tickers(page_size=3))
        self.assertEqual(len(rows), 4)
        self.assertIn("offset=3", c._opener.requests[1].full_url)

    def test_financial_indicators_report_format_passed_through(self):
        c = client([ok({"thscode": "600519.SH", "report": "2024-4", "abilities": []})])
        data = c.financial_indicators("600519.SH", "2024-4")
        self.assertEqual(data["report"], "2024-4")
        self.assertIn("report=2024-4", c._opener.requests[0].full_url)


if __name__ == "__main__":
    unittest.main()


class TestDoctor(unittest.TestCase):
    """自检命令必须报告失败而不是崩掉。"""

    def test_reports_ok_when_all_probes_pass(self):
        from jingshui.cli import cmd_doctor

        c = client([ok({"item": [{"thscode": "600519.SH"}]})] * 11)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cmd_doctor(None, client=c)
        self.assertEqual(rc, 0)
        self.assertNotIn("[FAIL]", out.getvalue())

    def test_reports_failures_without_raising(self):
        from jingshui.cli import cmd_doctor

        c = client([err(2003, "无权限")] * 11)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cmd_doctor(None, client=c)
        self.assertEqual(rc, 1)
        self.assertIn("code=2003", out.getvalue())

    def test_network_error_is_reported_as_unreachable(self):
        from jingshui.cli import cmd_doctor

        c = client([urllib.error.URLError("Tunnel connection failed: 403 Forbidden")] * 33)
        with mock.patch("time.sleep"), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cmd_doctor(None, client=c)
        self.assertEqual(rc, 1)
        self.assertIn("网络不可达", out.getvalue())


class TestHttpStatusErrors(unittest.TestCase):
    """HTTP 层的失败不能被误报成"网络不可达"。"""

    def test_429_is_not_reported_as_unreachable(self):
        c = client([http_error(429)])
        c.max_retries = 0
        with self.assertRaises(HttpStatusError) as ctx:
            c.price_snapshot(["600519.SH"])
        self.assertEqual(ctx.exception.status, 429)
        self.assertTrue(ctx.exception.retryable)

    def test_429_is_retried_then_succeeds(self):
        c = client([http_error(429), ok({"item": [{"thscode": "600519.SH"}]})])
        with mock.patch("time.sleep") as slept:
            rows = c.price_snapshot(["600519.SH"])
        self.assertEqual(len(rows), 1)
        self.assertTrue(slept.called)

    def test_retry_after_header_is_honoured(self):
        c = client([http_error(429, retry_after=7), ok({"item": []})])
        with mock.patch("time.sleep") as slept:
            c.price_snapshot(["600519.SH"])
        self.assertEqual(slept.call_args[0][0], 7.0)

    def test_retry_after_http_date_falls_back_to_backoff(self):
        c = client([http_error(429, retry_after="Wed, 21 Oct 2026 07:28:00 GMT"), ok({"item": []})])
        with mock.patch("time.sleep") as slept:
            c.price_snapshot(["600519.SH"])
        self.assertGreater(slept.call_args[0][0], 0.0)

    def test_403_is_not_retried(self):
        c = client([http_error(403, "Forbidden")])
        with self.assertRaises(HttpStatusError) as ctx:
            c.price_snapshot(["600519.SH"])
        self.assertEqual(ctx.exception.status, 403)
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(len(c._opener.requests), 1)

    def test_500_is_retried(self):
        c = client([http_error(500, "Server Error"), ok({"item": []})])
        with mock.patch("time.sleep"):
            self.assertEqual(c.price_snapshot(["600519.SH"]), [])

    def test_envelope_in_non_2xx_body_is_still_parsed(self):
        # 有些网关在 429 上仍带回业务信封, 应按 code 处理而不是当成 HTTP 错误
        body = json.dumps(err(4001, "限流"))
        c = client([http_error(429, body=body), ok({"item": []})])
        with mock.patch("time.sleep"):
            self.assertEqual(c.price_snapshot(["600519.SH"]), [])

    def test_body_is_captured_for_diagnosis(self):
        c = client([http_error(429, body="rate limit exceeded")])
        c.max_retries = 0
        with self.assertRaises(HttpStatusError) as ctx:
            c.price_snapshot(["600519.SH"])
        self.assertIn("rate limit exceeded", str(ctx.exception))


class TestDoctorThrottling(unittest.TestCase):
    def test_429_reported_as_throttling_not_unreachable(self):
        from jingshui.cli import cmd_doctor

        c = client([http_error(429)] * 40)
        c.max_retries = 0
        with mock.patch("time.sleep"), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cmd_doctor(None, client=c)
        text = out.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("HTTP 429", text)
        self.assertIn("限流", text)
        self.assertNotIn("网络不可达", text)
        self.assertIn("--pause", text)

    def test_business_rate_limit_code_also_counted_as_throttling(self):
        from jingshui.cli import cmd_doctor

        c = client([err(4001, "限流")] * 40)
        c.max_retries = 0
        with mock.patch("time.sleep"), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cmd_doctor(None, client=c)
        self.assertEqual(rc, 1)
        self.assertIn("限流", out.getvalue())

    def test_403_reported_as_auth_not_throttling(self):
        from jingshui.cli import cmd_doctor

        c = client([http_error(403, "Forbidden")] * 40)
        c.max_retries = 0
        with mock.patch("time.sleep"), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            cmd_doctor(None, client=c)
        text = out.getvalue()
        self.assertIn("鉴权或访问策略拒绝", text)
        self.assertNotIn("--pause", text)
