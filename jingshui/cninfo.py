"""巨潮资讯公开查询端点（零鉴权）：为入选股票提供「最新定期报告」与「上市招股说明书」PDF 直链。

同花顺金融数据服务官方声明不提供公告原文（docs/04），文档链接一律取自巨潮：

- orgId：POST /new/information/topSearch/query 按代码查询（gssz/gssh/gfbj 前缀）；
- 公告：POST /new/hisAnnouncement/query
  - 定期报告 = 年报/半年报/一季报/三季报四类合并，按披露时间倒序取第一条（排除摘要/英文版）；
  - 招股说明书 = searchkey=招股说明书 全文检索取第一条；
- PDF 直链：https://static.cninfo.com.cn/ + adjunctUrl；
- 交易所参数：深 column=szse / 沪 column=sse&plate=sh / 北 column=bj。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import requests

from .models import ms_to_date

log = logging.getLogger(__name__)

BASE = "https://www.cninfo.com.cn"
STATIC_PDF = "https://static.cninfo.com.cn/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PERIODIC_CATEGORIES = ("category_ndbg_szsh;category_bndbg_szsh;"
                       "category_yjdbg_szsh;category_sjdbg_szsh;")
EXCHANGE = {"SZ": ("szse", ""), "SH": ("sse", "sh"), "BJ": ("bj", "")}


def exchange_of(thscode: str) -> tuple[str, str]:
    suffix = thscode.partition(".")[2].upper()
    if suffix not in EXCHANGE:
        raise ValueError(f"未知交易所后缀：{thscode}")
    return EXCHANGE[suffix]


def pdf_url(adjunct: str) -> str:
    return STATIC_PDF + str(adjunct).lstrip("/")


def _doc(item: dict) -> dict | None:
    title = re.sub(r"<[^>]+>", "", str(item.get("announcementTitle") or "")).strip()
    adjunct = item.get("adjunctUrl")
    if not title or not adjunct:
        return None
    ts = item.get("announcementTime")
    return {
        "title": title,
        "date": ms_to_date(ts).strftime("%Y-%m-%d") if ts else None,
        "url": pdf_url(adjunct),
    }


def pick_report(items: list[dict]) -> dict | None:
    """定期报告按披露时间倒序列表，取第一条正文报告（排除摘要、英文版）。"""
    for item in items:
        d = _doc(item)
        if d and not any(x in d["title"] for x in ("摘要", "英文")):
            return d
    return None


def pick_prospectus(items: list[dict]) -> dict | None:
    for item in items:
        d = _doc(item)
        if d and "招股说明书" in d["title"] and not any(x in d["title"] for x in ("摘要", "提示")):
            return d
    return None


class CninfoClient:
    def __init__(self, min_interval: float = 0.5, retries: int = 3, timeout: float = 15.0,
                 session: requests.Session | None = None):
        self.min_interval = min_interval
        self.retries = retries
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": UA, "Referer": BASE + "/"})
        self._last = 0.0
        self.calls = 0

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _post(self, path: str, data: dict) -> Any:
        url = BASE + path
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(min(2 ** attempt, 10))
            self._throttle()
            self.calls += 1
            try:
                resp = self.session.post(url, data=data, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = RuntimeError(f"{path} 网络错误：{type(exc).__name__}")
                continue
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                last_exc = RuntimeError(f"{path} HTTP {resp.status_code}")
                continue
            try:
                return resp.json()
            except ValueError:
                last_exc = RuntimeError(f"{path} 非 JSON 响应")
                continue
        assert last_exc is not None
        raise last_exc

    def org_id(self, code6: str) -> str | None:
        data = self._post("/new/information/topSearch/query", {"keyWord": code6, "maxNum": 10})
        if not isinstance(data, list):
            return None
        for item in data:
            if str(item.get("code")) == code6 and item.get("orgId"):
                return str(item["orgId"])
        return None

    def _announcements(self, code6: str, org_id: str, column: str, plate: str,
                       category: str = "", searchkey: str = "", page_size: int = 30) -> list[dict]:
        data = self._post("/new/hisAnnouncement/query", {
            "pageNum": 1, "pageSize": page_size, "column": column, "tabName": "fulltext",
            "plate": plate, "stock": f"{code6},{org_id}", "searchkey": searchkey, "secid": "",
            "category": category, "trade": "", "seDate": "", "sortName": "", "sortType": "",
            "isHLtitle": "false",
        })
        return (data or {}).get("announcements") or []

    def latest_report(self, thscode: str, org_id: str) -> dict | None:
        column, plate = exchange_of(thscode)
        return pick_report(self._announcements(thscode.partition(".")[0], org_id, column, plate,
                                               category=PERIODIC_CATEGORIES, page_size=30))

    def prospectus(self, thscode: str, org_id: str) -> dict | None:
        column, plate = exchange_of(thscode)
        return pick_prospectus(self._announcements(thscode.partition(".")[0], org_id, column, plate,
                                                   searchkey="招股说明书", page_size=10))

    def docs(self, thscode: str) -> dict:
        """返回 {"report": {...}|None, "prospectus": {...}|None}。"""
        oid = self.org_id(thscode.partition(".")[0])
        if not oid:
            log.info("巨潮未找到 %s 的 orgId", thscode)
            return {"report": None, "prospectus": None}
        return {"report": self.latest_report(thscode, oid),
                "prospectus": self.prospectus(thscode, oid)}
