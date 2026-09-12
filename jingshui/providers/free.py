"""零鉴权备用数据源（--provider free），取数分工参考 simonlin1212/a-stock-data：

| 数据            | 源   | 端点                                                        |
|-----------------|------|-------------------------------------------------------------|
| 个股/指数日线   | 腾讯 | web.ifzq.gtimg.cn/appstock/app/fqkline/get（前复权）        |
| 流通市值        | 腾讯 | qt.gtimg.cn（字段 44=流通市值，45=总市值）                  |
| 财报（累计口径）| 新浪 | quotes.sina.cn/.../CompanyFinanceService.getFinanceReport2022 |
| 板块目录/成分   | 东财 | push2.eastmoney.com/api/qt/clist/get                        |
| 板块日线        | 东财 | push2his.eastmoney.com/api/qt/stock/kline/get               |

与同花顺源的实质差异：
1. 无龙虎榜机构净额 → I 因子只剩成交额部分；
2. 新浪财报无可靠披露日 → 按法定披露截止日推算（一季报 4/30、半年报 8/31、三季报 10/31、年报次年 4/30）；
3. 腾讯日线只有成交量（手）→ 成交额按 量×均价 估算。

东财必须串行限流：该项目记录过不走限流的并发脚本被 IP 级封禁 20 小时以上。
这里所有东财请求走 em_get()：串行、间隔 ≥1.2 秒 + 抖动，撞 403/429 间隔翻倍。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

from ..models import PricePanel, long_to_panel
from .base import Provider

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

SH_INDEX = {"000300", "000905", "000016", "000688", "000852", "000010", "000001"}


def to_prefixed(thscode: str) -> str:
    code, _, ex = thscode.partition(".")
    return f"{ex.lower()}{code}" if ex else code


def suffix_for(code: str) -> str:
    if code.startswith(("92", "4", "8")):
        return f"{code}.BJ"
    if code.startswith(("5", "6", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


# ---------------- 东财：串行限流 ----------------
class _EM:
    interval = 1.2
    _last = 0.0
    _lock = threading.Lock()
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})

    @classmethod
    def get(cls, url: str, params: dict, timeout: int = 15) -> dict:
        with cls._lock:  # 串行：同一时刻只有一个东财请求
            for attempt in range(4):
                wait = cls.interval - (time.monotonic() - cls._last)
                if wait > 0:
                    time.sleep(wait + random.uniform(0.1, 0.5))
                try:
                    r = cls.session.get(url, params=params, timeout=timeout)
                except requests.RequestException as exc:
                    log.warning("东财网络错误（%s），第 %d 次", type(exc).__name__, attempt + 1)
                    time.sleep(2 + attempt * 3)
                    continue
                finally:
                    cls._last = time.monotonic()
                if r.status_code in (403, 429):
                    cls.interval = min(cls.interval * 2, 30)
                    log.warning("东财返回 %s，限流间隔调到 %.1fs", r.status_code, cls.interval)
                    continue
                r.raise_for_status()
                return r.json()
        raise RuntimeError("东财请求持续被拒绝（可能已被风控），请稍后再试或切回 hithink")


def em_clist(fs: str, fields: str, pz: int = 100) -> list[dict]:
    rows, pn = [], 1
    while True:
        d = _EM.get("https://push2.eastmoney.com/api/qt/clist/get", {
            "pn": pn, "pz": pz, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f12",
            "fs": fs, "fields": fields})
        data = d.get("data") or {}
        diff = data.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        rows += diff
        total = data.get("total") or 0
        if not diff or len(rows) >= total:
            return rows
        pn += 1


# ---------------- 腾讯 ----------------
_TX = requests.Session()
_TX.headers.update({"User-Agent": UA, "Referer": "https://gu.qq.com/"})


def tencent_kline(prefixed: str, count: int = 640, adjust: str = "qfq") -> pd.DataFrame:
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    for attempt in range(3):
        try:
            r = _TX.get(url, params={"param": f"{prefixed},day,,,{count},{adjust}"}, timeout=15)
            node = ((r.json().get("data") or {}).get(prefixed)) or {}
            rows = node.get(f"{adjust}day") or node.get("day") or []
            break
        except (requests.RequestException, ValueError):
            time.sleep(1 + attempt * 2)
    else:
        return pd.DataFrame()
    recs = []
    for x in rows:
        if len(x) < 6:
            continue
        o, c, h, l, v = (float(x[i]) for i in (1, 2, 3, 4, 5))
        recs.append({"date": pd.Timestamp(x[0]), "open": o, "close": c, "high": h, "low": l,
                     "volume": v * 100, "turnover": v * 100 * (h + l + c) / 3})
    return pd.DataFrame(recs)


def tencent_float_mcap(codes: list[str]) -> dict[str, float]:
    """thscode → 流通市值（亿元）。腾讯字段 44=流通市值、45=总市值。"""
    out: dict[str, float] = {}
    for i in range(0, len(codes), 60):
        chunk = codes[i:i + 60]
        keymap = {to_prefixed(c): c for c in chunk}
        r = _TX.get("https://qt.gtimg.cn/q=" + ",".join(keymap), timeout=10)
        for line in r.content.decode("gbk", "ignore").split(";"):
            if '"' not in line or "=" not in line:
                continue
            key = line.split("=")[0].strip().split("_")[-1]
            vals = line.split('"')[1].split("~")
            if len(vals) > 45 and key in keymap and vals[44]:
                try:
                    out[keymap[key]] = float(vals[44])
                except ValueError:
                    pass
    return out


# ---------------- 新浪财报 ----------------
def sina_report(thscode: str, source: str, num: int = 12) -> list[dict]:
    r = requests.get("https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022",
                     params={"paperCode": to_prefixed(thscode), "source": source, "type": "0",
                             "page": "1", "num": str(num)},
                     headers={"User-Agent": UA}, timeout=15)
    j = r.json() or {}
    report_list = ((j.get("result") or {}).get("data") or {}).get("report_list") or {}
    out = []
    for period in sorted(report_list, reverse=True):
        obj = report_list[period] or {}
        rec = {"period": period, "publish_date": obj.get("publish_date")}
        for it in obj.get("data") or []:
            title, val = it.get("item_title"), it.get("item_value")
            if title and val not in (None, ""):
                try:
                    rec[title] = float(val)
                except (TypeError, ValueError):
                    pass
        out.append(rec)
    return out


def _pick(rec: dict, *names):
    for n in names:
        if rec.get(n) is not None:
            return rec[n]
    return None


def _deadline(period_end: pd.Timestamp) -> str:
    """新浪不给披露日时，按法定披露截止日推算（保守：宁晚勿早，避免未来函数）。"""
    y, m = period_end.year, period_end.month
    return {3: f"{y}-04-30", 6: f"{y}-08-31", 9: f"{y}-10-31", 12: f"{y + 1}-04-30"}[m]


class FreeProvider(Provider):
    name = "free"
    supports_org_flow = False

    def __init__(self, cfg: dict, cache):
        self.cfg = cfg
        self.cache = cache
        self._tdays = None
        self._sector_codes: dict[str, str] = {}

    def trading_days(self) -> list[pd.Timestamp]:
        if self._tdays is None:
            df = tencent_kline("sh000001", 260, adjust="")
            self._tdays = list(df["date"])
        return self._tdays

    def universe(self) -> pd.DataFrame:
        def fetch():
            rows = em_clist("m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048", "f12,f14")
            return pd.DataFrame([{"thscode": suffix_for(str(x["f12"])), "name": x.get("f14", ""),
                                  "list_date": None} for x in rows])
        return self.cache.frame("universe_free", 1, fetch)

    def price_panel(self, asof: pd.Timestamp, keep_days: int) -> PricePanel:
        codes = list(self.universe()["thscode"])
        count = min(keep_days, 640)

        def one(c):
            df = tencent_kline(to_prefixed(c), count)
            if not df.empty:
                df["thscode"] = c
            return df

        frames, done = [], 0
        with ThreadPoolExecutor(4) as ex:
            for df in ex.map(one, codes):
                done += 1
                if done % 500 == 0:
                    log.info("腾讯日线 %d/%d", done, len(codes))
                if not df.empty:
                    frames.append(df)
        long = pd.concat(frames, ignore_index=True)
        long = long[long["date"] <= asof]
        panel = long_to_panel(long, "free")  # 腾讯已是前复权
        panel.notes = ["free 数据源：成交额按 量×均价 估算；无龙虎榜机构净额"]
        return panel

    def index_bars(self, code: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        if code.endswith(".TI") or code.startswith("BK"):
            bk = self._sector_codes.get(code, code)
            d = _EM.get("https://push2his.eastmoney.com/api/qt/stock/kline/get", {
                "secid": f"90.{bk}", "klt": 101, "fqt": 0, "lmt": 600, "end": "20500101",
                "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57"})
            rows = []
            for line in ((d.get("data") or {}).get("klines") or []):
                p = line.split(",")
                rows.append({"date": pd.Timestamp(p[0]), "open": float(p[1]), "close": float(p[2]),
                             "high": float(p[3]), "low": float(p[4]), "volume": float(p[5]),
                             "turnover": float(p[6])})
            df = pd.DataFrame(rows)
        else:
            df = tencent_kline(to_prefixed(code), 640, adjust="")
        if df.empty:
            return df
        return df[(df["date"] >= start) & (df["date"] <= end)].sort_values("date")

    def sector_list(self) -> pd.DataFrame:
        rows = em_clist("m:90+t:2", "f12,f14")
        df = pd.DataFrame([{"code": x["f12"], "name": x["f14"]} for x in rows])
        # 东财行业板块混有一/二/三级（2026-09 实测 496 个，三级名称带"Ⅲ"后缀）。
        # 优先只取二级（名称带"Ⅱ"），数量异常时退回全部。
        lvl2 = df[df["name"].str.endswith("Ⅱ")]
        if len(lvl2) >= 30:
            df = lvl2.reset_index(drop=True)
        self._sector_codes = {c: c for c in df["code"]}
        return df

    def sector_constituents(self, code: str) -> pd.DataFrame:
        rows = em_clist(f"b:{code}", "f12,f14")
        return pd.DataFrame([{"thscode": suffix_for(str(x["f12"])), "name": x.get("f14", "")} for x in rows])

    def financials(self, thscode: str) -> dict:
        inc = sina_report(thscode, "lrb", 12)
        bal = sina_report(thscode, "fzb", 12)
        quarterly = []
        for rec in inc:
            pe = pd.Timestamp(rec["period"])
            if pe.month not in (3, 6, 9, 12):
                continue
            quarterly.append({
                "period_end": str(pe.date()),
                "report_date": rec.get("publish_date") or _deadline(pe),
                "fiscal_year": pe.year, "quarter": pe.month // 3,
                "revenue": _pick(rec, "营业总收入", "营业收入"),
                "parent_np": _pick(rec, "归属于母公司所有者的净利润", "归属于母公司股东的净利润",
                                   "归属于母公司的净利润"),
                "eps": _pick(rec, "基本每股收益"),
            })
        annual = [q for q in quarterly if q["quarter"] == 4]
        balance = []
        for rec in bal:
            pe = pd.Timestamp(rec["period"])
            if pe.month != 12:
                continue
            balance.append({
                "period_end": str(pe.date()), "report_date": rec.get("publish_date") or _deadline(pe),
                "fiscal_year": pe.year,
                "equity": _pick(rec, "归属于母公司股东权益合计", "归属于母公司所有者权益合计",
                                "所有者权益(或股东权益)合计", "所有者权益合计"),
            })
        return {"quarterly": quarterly, "annual": annual, "balance": balance}

    def financials_many(self, codes: list[str]) -> dict[str, dict | None]:
        out = {}
        for c in codes:
            try:
                out[c] = self.financials(c)
            except Exception as exc:  # 公开接口波动大，单只失败不影响整体
                log.warning("新浪财报失败 %s: %s", c, exc)
                out[c] = None
            time.sleep(0.2)
        return out

    def valuations(self, codes: list[str]) -> pd.DataFrame:
        return super().valuations(codes)
