"""零鉴权公开数据源。

来源与分工参考 simonlin1212/a-stock-data 的整理:

| 数据 | 源 | 说明 |
|---|---|---|
| 个股/宽基指数日线 | 腾讯 `web.ifzq.gtimg.cn` | 前复权, 不封 IP |
| 财报三表 | 新浪 `quotes.sina.cn` | 零鉴权, 累计口径 |
| 板块目录/成分/日线 | 东财 `push2` / `push2his` | **有 IP 级风控, 必须串行限流** |

方法签名与 HithinkClient 完全一致, 因此 pipeline 无需区分。

与 hithink 数据源的三处实质差异, 使用前必须知道:

1. **没有龙虎榜机构净额**。东财公开的全市场龙虎榜给的是总净额而非机构席位
   净额, 语义不同, 与其塞一个意思不一样的数进去, 不如不给。因此 I 因子的
   6 分机构分项在本数据源下恒为 0, 只保留 4 分的成交额分项, 个股总分上限
   从 100 降到 94, 入选线 70 相对更严。
2. **披露日是推算的**。新浪财报不返回实际披露日, 这里按法定截止日推算
   (一季报 4/30、半年报 8/31、三季报 10/31、年报次年 4/30)。方向上偏保守
   —— 假设数据比实际更晚才可见, 因此不会引入未来函数, 但会让刚出的财报
   晚几天才进入筛选。
3. **成交额是估算的**。腾讯日线不返回成交额, 这里用 收盘价 x 成交量 近似,
   与真实成交额 (按成交均价) 有偏差。它只用于流动性门槛, 量级足够。
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import Any

from ._http import FetchError, Throttle, fetch_json

LOG = logging.getLogger(__name__)

TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_FINANCE = (
    "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
)
EM_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

# 东财板块类别 -> clist 的 fs 过滤串
SECTOR_TAGS = {"industry": "m:90+t:2", "cn_concept": "m:90+t:3", "region": "m:90+t:1"}

SH_INDEX_WHITELIST = {"000001", "000300", "000905", "000016", "000688", "000852", "000010"}

_SECTOR_SUFFIX = ".EM"


# ---------- 代码转换 ----------

def to_tencent_code(thscode: str, is_index: bool = False) -> str:
    """`600519.SH` / `600519` -> `sh600519`。"""
    raw = thscode.strip()
    low = raw.lower()
    if low.startswith(("sh", "sz", "bj")):
        return low
    code, _, suffix = raw.partition(".")
    suffix = suffix.upper()
    if suffix in ("SH", "SZ", "BJ"):
        return f"{suffix.lower()}{code}"
    if is_index and code in SH_INDEX_WHITELIST:
        return f"sh{code}"
    if code.startswith("92"):
        return f"bj{code}"
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def to_em_secid(thscode: str) -> str:
    """东财 secid: 沪=1, 深/北=0; 板块=90。"""
    raw = thscode.strip()
    if raw.upper().endswith(_SECTOR_SUFFIX):
        return f"90.{raw[: -len(_SECTOR_SUFFIX)]}"
    if raw.upper().startswith("BK"):
        return f"90.{raw.split('.')[0]}"
    tencent = to_tencent_code(raw)
    market = "1" if tencent.startswith("sh") else "0"
    return f"{market}.{tencent[2:]}"


def is_sector_code(thscode: str) -> bool:
    upper = thscode.strip().upper()
    return upper.endswith(_SECTOR_SUFFIX) or upper.startswith("BK")


def _plain_code(thscode: str) -> str:
    return thscode.strip().split(".")[0].lstrip("shzbj") or thscode.strip().split(".")[0]


# ---------- 时间 ----------

def _ms(date_str: str) -> int:
    d = _dt.datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
    return int(d.timestamp() * 1000)


def _ymd(ms: int) -> str:
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime("%Y%m%d")


def statutory_disclosure_ms(year: int, quarter: int) -> int:
    """A 股定期报告的法定披露截止日。

    新浪财报不给实际披露日, 用截止日代替。这个方向是保守的: 假设数据比
    实际更晚可见, 因此不会引入未来函数。
    """
    mapping = {1: (year, 4, 30), 2: (year, 8, 31), 3: (year, 10, 31), 4: (year + 1, 4, 30)}
    y, m, d = mapping[quarter]
    return int(_dt.datetime(y, m, d, tzinfo=_dt.timezone.utc).timestamp() * 1000)


# ---------- 解析 ----------

def _num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in ("-", "--", "None", "null"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_tencent_klines(payload: dict, tencent_code: str) -> list[dict]:
    """把腾讯 fqkline 的响应转成 HiThink 的 K 线行结构。

    行结构是 [日期, 开, **收**, 高, 低, 量]。收盘价在第三位而不是最后一位,
    是这个接口最容易搞错的地方, 所以下面用 OHLC 自洽性做了校验: 一旦字段
    顺序不对, 高低价必然穿越开收价, 与其静默产出错数据不如直接报错。
    """
    data = (payload or {}).get("data") or {}
    node = data.get(tencent_code) or {}
    rows = None
    for key in ("qfqday", "day", "hfqday", "qfqweek"):
        candidate = node.get(key)
        if isinstance(candidate, list) and candidate:
            rows = candidate
            break
    if rows is None:
        return []

    out: list[dict] = []
    violations = 0
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        date_raw = str(row[0])[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_raw):
            continue
        o, c, h, low_, v = (_num(row[1]), _num(row[2]), _num(row[3]), _num(row[4]), _num(row[5]))
        if None in (o, c, h, low_, v):
            continue
        if h < max(o, c) - 1e-9 or low_ > min(o, c) + 1e-9:
            violations += 1
            continue
        # 腾讯日线不返回成交额, 用 收盘价 x 成交量 估算 (成交量单位是手)
        out.append(
            {
                "date_ms": _ms(date_raw),
                "open_price": o,
                "high_price": h,
                "low_price": low_,
                "close_price": c,
                "volume": v * 100.0,
                "turnover": c * v * 100.0,
            }
        )

    if out and violations > len(rows) * 0.1:
        raise FetchError(
            f"腾讯 K 线有 {violations}/{len(rows)} 根不满足 高>=max(开,收) 且 低<=min(开,收), "
            "字段顺序可能已变更, 拒绝返回可能错位的数据"
        )
    if not out and violations:
        raise FetchError("腾讯 K 线全部未通过 OHLC 自洽性校验, 字段顺序可能已变更")
    out.sort(key=lambda r: r["date_ms"])
    return out


def parse_em_klines(payload: dict) -> list[dict]:
    """东财 kline: data.klines 每行是 "日期,开,收,高,低,量,额,振幅"。"""
    data = (payload or {}).get("data") or {}
    lines = data.get("klines") or []
    out: list[dict] = []
    for line in lines:
        parts = str(line).split(",")
        if len(parts) < 7:
            continue
        o, c, h, low_, v, amt = (_num(parts[1]), _num(parts[2]), _num(parts[3]),
                                 _num(parts[4]), _num(parts[5]), _num(parts[6]))
        if None in (o, c, h, low_, v):
            continue
        if h < max(o, c) - 1e-9 or low_ > min(o, c) + 1e-9:
            continue
        out.append(
            {
                "date_ms": _ms(parts[0]),
                "open_price": o,
                "high_price": h,
                "low_price": low_,
                "close_price": c,
                "volume": v * 100.0,
                "turnover": amt if amt is not None else c * v * 100.0,
            }
        )
    out.sort(key=lambda r: r["date_ms"])
    return out


# 新浪财报科目名 -> HiThink 字段。同一含义可能有多个写法, 取第一个命中的。
SINA_INCOME_FIELDS = {
    "parent_holder_net_profit": (
        "归属于母公司所有者的净利润",
        "归属于母公司股东的净利润",
        "五、净利润",
        "净利润",
    ),
    "net_profit": ("净利润", "五、净利润"),
    "operating_income": ("营业总收入", "一、营业总收入", "营业收入", "一、营业收入"),
    "operating_costs": ("营业成本", "营业总成本"),
    "basic_eps": ("基本每股收益", "(一)基本每股收益"),
}
SINA_BALANCE_FIELDS = {
    "holder_equity_total": (
        "所有者权益(或股东权益)合计",
        "所有者权益合计",
        "股东权益合计",
        "所有者权益（或股东权益）合计",
    ),
    "assets_total": ("资产总计", "资产总额"),
    "total_debt": ("负债合计", "负债总计"),
}


def _pick(record: dict, names: tuple[str, ...]) -> float | None:
    for name in names:
        if name in record:
            value = _num(record[name])
            if value is not None:
                return value
    return None


def parse_sina_reports(rows: list[dict], thscode: str, mapping: dict) -> list[dict]:
    """把新浪财报行转成 HiThink 的报表结构 (累计口径, 按报告期倒序)。"""
    out: list[dict] = []
    for rec in rows or []:
        period = str(rec.get("报告期", ""))[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", period):
            continue
        year = int(period[:4])
        month = int(period[5:7])
        quarter = {3: 1, 6: 2, 9: 3, 12: 4}.get(month)
        if quarter is None:
            continue
        item = {
            "thscode": thscode,
            "ticker": _plain_code(thscode),
            "period": "annual" if quarter == 4 else "quarterly",
            "period_end_ms": _ms(period),
            "report_date_ms": statutory_disclosure_ms(year, quarter),
            "report_date_is_estimated": True,
            "fiscal_year": year,
            "fiscal_period": {1: "Q1", 2: "H1", 3: "Q3", 4: "FY"}[quarter],
            "currency": "CNY",
        }
        for field, names in mapping.items():
            item[field] = _pick(rec, names)
        out.append(item)
    out.sort(key=lambda r: r["period_end_ms"], reverse=True)
    return out


def parse_sina_envelope(payload: dict) -> list[dict]:
    """新浪的 report_list 是以报告期为键的 dict, 每期的 data 才是行项列表。"""
    result = ((payload or {}).get("result") or {}).get("data") or {}
    report_list = result.get("report_list") or {}
    rows: list[dict] = []
    for period in sorted(report_list.keys(), reverse=True):
        obj = report_list[period] or {}
        rec: dict[str, Any] = {"报告期": f"{period[:4]}-{period[4:6]}-{period[6:8]}"}
        for entry in obj.get("data") or []:
            title = entry.get("item_title")
            if title and entry.get("item_value") is not None:
                rec[title] = entry["item_value"]
        rows.append(rec)
    return rows


class FreeProvider:
    """零鉴权数据源。方法签名与 HithinkClient 一致。"""

    name = "free"

    def __init__(self, throttle_scale: float = 1.0, opener: Any = None, timeout: float = 15.0) -> None:
        self.throttle = Throttle(scale=throttle_scale)
        self._opener = opener
        self.timeout = timeout

    # 与 HithinkClient 的接口对齐: pipeline 会调用它来设置节流下限
    def set_min_interval(self, seconds: float) -> None:
        if seconds > 0:
            self.throttle.scale = max(self.throttle.scale, seconds / 0.3)

    def _get(self, url: str, params: dict | None = None, headers: dict | None = None,
             encoding: str = "utf-8") -> Any:
        return fetch_json(
            url, params=params, headers=headers, throttle=self.throttle,
            timeout=self.timeout, encoding=encoding, opener=self._opener,
        )

    # ---------- 行情 ----------

    def _klines(self, thscode: str, start_ms: int, end_ms: int, is_index: bool) -> list[dict]:
        if is_sector_code(thscode):
            payload = self._get(
                EM_KLINE,
                {
                    "secid": to_em_secid(thscode),
                    "klt": 101, "fqt": 1,
                    "beg": _ymd(start_ms), "end": _ymd(end_ms),
                    "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                },
            )
            rows = parse_em_klines(payload)
        else:
            code = to_tencent_code(thscode, is_index=is_index)
            count = max(64, min(1200, int((end_ms - start_ms) / 86_400_000)))
            # 指数没有复权概念, 不要传 qfq
            param = f"{code},day,,,{count}" + ("" if is_index else ",qfq")
            payload = self._get(TENCENT_KLINE, {"param": param},
                                headers={"Referer": "https://gu.qq.com/"})
            rows = parse_tencent_klines(payload, code)
        return [r for r in rows if start_ms <= r["date_ms"] <= end_ms]

    def price_history(self, thscode: str, start_ms: int, end_ms: int,
                      adjust: str = "forward", interval: str = "1d") -> list[dict]:
        return self._klines(thscode, start_ms, end_ms, is_index=False)

    def index_history(self, thscode: str, start_ms: int, end_ms: int,
                      interval: str = "1d") -> list[dict]:
        return self._klines(thscode, start_ms, end_ms, is_index=True)

    # ---------- 板块 ----------

    def index_catalog(self, tag: str = "industry") -> list[dict]:
        fs = SECTOR_TAGS.get(tag)
        if fs is None:
            raise ValueError(f"未知板块类别 {tag!r}, 可选: {', '.join(SECTOR_TAGS)}")
        payload = self._get(
            EM_CLIST,
            {"pn": 1, "pz": 500, "po": 1, "np": 1, "fltt": 2, "invt": 2,
             "fid": "f3", "fs": fs, "fields": "f12,f14"},
        )
        items = ((payload or {}).get("data") or {}).get("diff") or []
        if isinstance(items, dict):  # 东财偶尔以 dict 返回
            items = list(items.values())
        return [
            {"thscode": f"{row['f12']}{_SECTOR_SUFFIX}", "name": row.get("f14", row["f12"])}
            for row in items
            if row.get("f12")
        ]

    def index_constituents(self, thscode: str) -> list[dict]:
        code = thscode.strip()
        if code.upper().endswith(_SECTOR_SUFFIX):
            code = code[: -len(_SECTOR_SUFFIX)]
        payload = self._get(
            EM_CLIST,
            {"pn": 1, "pz": 300, "po": 1, "np": 1, "fltt": 2, "invt": 2,
             "fid": "f3", "fs": f"b:{code}", "fields": "f12,f13,f14"},
        )
        items = ((payload or {}).get("data") or {}).get("diff") or []
        if isinstance(items, dict):
            items = list(items.values())
        out = []
        for row in items:
            ticker = row.get("f12")
            if not ticker:
                continue
            suffix = "SH" if str(row.get("f13")) == "1" else "SZ"
            out.append({
                "thscode": f"{ticker}.{suffix}",
                "ticker": ticker,
                "name": row.get("f14", ticker),
            })
        return out

    # ---------- 财务 ----------

    def _sina_report(self, thscode: str, source: str, limit: int) -> list[dict]:
        code = _plain_code(thscode)
        tencent = to_tencent_code(thscode)
        payload = self._get(
            SINA_FINANCE,
            {"paperCode": f"{tencent[:2]}{code}", "source": source,
             "type": "0", "page": "1", "num": str(max(limit, 8))},
        )
        return parse_sina_envelope(payload)

    def income_statements(self, thscode: str, period: str = "quarterly",
                          limit: int = 12) -> list[dict]:
        rows = parse_sina_reports(
            self._sina_report(thscode, "lrb", limit + 8), thscode, SINA_INCOME_FIELDS
        )
        if period == "annual":
            rows = [r for r in rows if r["fiscal_period"] == "FY"]
        return rows[:limit]

    def balance_sheets(self, thscode: str, period: str = "annual",
                       limit: int = 5) -> list[dict]:
        rows = parse_sina_reports(
            self._sina_report(thscode, "fzb", limit + 8), thscode, SINA_BALANCE_FIELDS
        )
        if period == "annual":
            rows = [r for r in rows if r["fiscal_period"] == "FY"]
        return rows[:limit]

    # ---------- 不支持的能力 ----------

    def dragon_tiger(self, board_type: str = "org", date: str | None = None) -> dict:
        """本数据源不提供龙虎榜**机构席位**净额。

        东财公开的全市场龙虎榜给的是总净额, 不是机构净额, 语义不同。
        与其塞一个意思不一样的数进去, 不如明确返回空 —— I 因子会据此
        降级并在理由里说明, 而不是拿总净额冒充机构动向。
        """
        return {"stock_items": [], "unsupported": "free 数据源没有机构席位净额"}
