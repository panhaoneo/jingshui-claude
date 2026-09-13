"""守住巨潮公告链接：交易所映射、报告/招股书筛选、端点调用纪律（重试后放弃）。"""
import pandas as pd
import pytest

from jingshui.cninfo import CninfoClient, exchange_of, pdf_url, pick_prospectus, pick_report


def _ms(day: str) -> int:
    return pd.Timestamp(day, tz="Asia/Shanghai").value // 10**6


def _ann(title, adjunct="finalpage/2026-08-20/1217.PDF", ts=None):
    return {"announcementTitle": title, "adjunctUrl": adjunct, "announcementTime": ts}


def test_exchange_of():
    assert exchange_of("000001.SZ") == ("szse", "")
    assert exchange_of("600519.SH") == ("sse", "sh")
    assert exchange_of("920185.BJ") == ("bj", "")
    assert exchange_of("830799.bj") == ("bj", "")
    with pytest.raises(ValueError):
        exchange_of("000001.XX")


def test_pdf_url():
    assert pdf_url("finalpage/2026-08-20/1217.PDF") == "https://static.cninfo.com.cn/finalpage/2026-08-20/1217.PDF"
    assert pdf_url("/finalpage/x.PDF") == "https://static.cninfo.com.cn/finalpage/x.PDF"


def test_pick_report_skips_summary_and_english_versions():
    items = [_ann("2026年半年度报告摘要"), _ann("2026年半年度报告（英文版）"),
             _ann("2026年半年度报告", ts=_ms("2026-08-20")), _ann("2025年年度报告", ts=_ms("2026-03-30"))]
    assert pick_report(items) == {"title": "2026年半年度报告", "date": "2026-08-20",
                                  "url": "https://static.cninfo.com.cn/finalpage/2026-08-20/1217.PDF"}
    assert pick_report([_ann("2026年半年度报告摘要")]) is None
    assert pick_report([_ann("无链接", adjunct=None)]) is None


def test_pick_prospectus_filters_abstracts_and_strips_tags():
    items = [_ann("招股说明书摘要"), _ann("招股说明书提示性公告"),
             _ann("<em>首次公开发行股票并在主板上市招股说明书</em>",
                  adjunct="finalpage/2016-07-05/99.PDF", ts=_ms("2016-07-05"))]
    assert pick_prospectus(items) == {
        "title": "首次公开发行股票并在主板上市招股说明书", "date": "2016-07-05",
        "url": "https://static.cninfo.com.cn/finalpage/2016-07-05/99.PDF"}
    assert pick_prospectus([_ann("上市公告书")]) is None


class FakeResp:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, org=(), reports=(), prospectus=(), fail_times=0):
        self.headers = {}
        self.calls = []
        self.org, self.reports, self.prospectus = list(org), list(reports), list(prospectus)
        self.fail_times = fail_times

    def post(self, url, data=None, timeout=None):
        self.calls.append((url.split("/new/")[-1], dict(data)))
        if self.fail_times:
            self.fail_times -= 1
            return FakeResp(None, status=429)
        if url.endswith("topSearch/query"):
            return FakeResp(self.org)
        if data.get("category"):
            return FakeResp({"announcements": self.reports})
        return FakeResp({"announcements": self.prospectus})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("jingshui.cninfo.time.sleep", lambda s: None)


REPORTS = [_ann("2026年半年度报告摘要"), _ann("2026年半年度报告", ts=_ms("2026-08-20"))]
PROSPECTUS = [_ann("首次公开发行股票并在主板上市招股说明书", adjunct="finalpage/2016-07-05/99.PDF",
                   ts=_ms("2016-07-05"))]


def make(**kw):
    s = FakeSession(**kw)
    return CninfoClient(min_interval=0, session=s), s


def test_docs_flow_uses_exchange_columns_and_stock_pair():
    cl, s = make(org=[{"code": "000001", "orgId": "gssz0000001"}], reports=REPORTS, prospectus=PROSPECTUS)
    d = cl.docs("000001.SZ")
    assert d["report"]["title"] == "2026年半年度报告" and d["report"]["date"] == "2026-08-20"
    assert d["prospectus"]["title"] == "首次公开发行股票并在主板上市招股说明书"
    assert [p for p, _ in s.calls] == ["information/topSearch/query", "hisAnnouncement/query", "hisAnnouncement/query"]
    rep_kw, pros_kw = s.calls[1][1], s.calls[2][1]
    assert rep_kw["column"] == "szse" and rep_kw["plate"] == "" and rep_kw["stock"] == "000001,gssz0000001"
    assert rep_kw["category"].count("category_") == 4 and rep_kw["searchkey"] == ""
    assert pros_kw["searchkey"] == "招股说明书" and pros_kw["category"] == ""


def test_docs_sh_uses_sse_plate_and_empty_result_is_none():
    cl, s = make(org=[{"code": "600519", "orgId": "gssh0600519"}])
    assert cl.docs("600519.SH") == {"report": None, "prospectus": None}
    assert s.calls[1][1]["column"] == "sse" and s.calls[1][1]["plate"] == "sh"


def test_docs_without_org_id_skips_announcements():
    cl, s = make(org=[{"code": "999999", "orgId": "gsxx"}])
    assert cl.docs("000001.SZ") == {"report": None, "prospectus": None}
    assert len(s.calls) == 1


def test_rate_limit_retried_then_succeeds():
    cl, s = make(org=[{"code": "000001", "orgId": "gssz0000001"}], reports=REPORTS, fail_times=2)
    assert cl.docs("000001.SZ")["report"]["title"] == "2026年半年度报告"
    assert len(s.calls) == 5  # topSearch 重试 2 次 + 2 次公告查询


def test_http_error_gives_up_after_retries():
    cl, s = make(fail_times=99)
    with pytest.raises(RuntimeError):
        cl.org_id("000001")
    assert len(s.calls) == 4  # 1 次 + 3 次重试
