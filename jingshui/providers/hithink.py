"""同花顺金融数据服务数据源。

全市场日线用 Market Dumps（3 次请求拿全市场），不逐只拉 K 线：
- 首次 / 本地落后 >7 个交易日：daily-k（10 年全量，约 180MB）→ 只保留最近 keep_days 个交易日；
- 日常：daily-k-10d 增量，按 (thscode, date) 去重合并；
- dump 尚未发布当日数据时，用全市场快照补当日 K 线（仅收盘后）；
- 复权事件 dump 每次全量下载（约 0.3MB），本地推算前复权。
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import time as dt_time

import pandas as pd

from ..adjust import forward_factors
from ..client import ApiError, HiThinkClient
from ..models import PricePanel, date_to_ms, long_to_panel, ms_to_date
from .base import Provider

log = logging.getLogger(__name__)

KLINE_COLS = ["thscode", "date", "open", "high", "low", "close", "volume", "turnover"]
QMAP = {"Q1": 1, "Q2": 2, "H1": 2, "Q3": 3, "Q4": 4, "FY": 4}


def snapshot_covers(snap_time: pd.Timestamp, asof: pd.Timestamp, tdays: list[pd.Timestamp]) -> bool:
    """快照时刻的行情是否就是 asof 当日的完整日线。

    快照在收盘后（≥15:00）到下一交易日集合竞价（09:15）之前，反映的都是最近一个
    已收盘交易日的完整行情；非交易日（周末/节假日）全天如此。定时任务被 GitHub
    延迟到次日凌晨触发时，用它仍能补出前一交易日的 K 线。
    """
    snap_date = snap_time.normalize()
    last_closed = None
    for d in tdays:
        if d > snap_date or (d == snap_date and snap_time.time() < dt_time(15, 0)):
            break
        last_closed = d
    if last_closed != asof:
        return False
    if snap_date == asof:
        return True
    return snap_date not in set(tdays) or snap_time.time() < dt_time(9, 15)


def recent_missing_days(store_dates, window: list[pd.Timestamp]) -> list[pd.Timestamp]:
    """给定交易日窗口，行情库本地没有 K 线的那些日期。"""
    have = set(pd.DatetimeIndex(store_dates).unique())
    return [d for d in window if d not in have]


class HiThinkProvider(Provider):
    name = "hithink"
    supports_org_flow = True

    def __init__(self, cfg: dict, cache):
        self.cfg = cfg
        self.cache = cache
        d = cfg["data"]
        self.client = HiThinkClient(min_interval=d.get("request_interval", 0.12))
        self.max_workers = d.get("max_workers", 4)
        self._tdays: list[pd.Timestamp] | None = None
        self._store: pd.DataFrame | None = None
        self._events: pd.DataFrame | None = None
        self._patch_tried = False

    # ---------- 基础 ----------
    def trading_days(self) -> list[pd.Timestamp]:
        if self._tdays is None:
            data = self.client.get("/api/a-share/calendar/trading-days")
            self._tdays = [pd.Timestamp(x["date"]) for x in data["item"]]
        return self._tdays

    def universe(self) -> pd.DataFrame:
        def fetch():
            rows, offset = [], 0
            while True:
                data = self.client.get("/api/meta/tickers/list", {
                    "exchange": "SH,SZ,BJ", "asset_type": "a-share", "limit": 5000, "offset": offset})
                items = data.get("item") or []
                rows += [{"thscode": x["thscode"], "name": x.get("name") or "",
                          "list_date": x.get("list_date")} for x in items]
                if len(items) < 5000:
                    break
                offset += 5000
            return pd.DataFrame(rows)
        return self.cache.frame("universe", 1, fetch)

    # ---------- 全市场日线 ----------
    def _dump(self, kind: str):
        tags = []

        def sign() -> str:
            url = self.client.get(f"/api/dump/market-dumps/{kind}/download-url")["presigned_url"]
            m = re.search(r"_(\d{8})\.parquet", url.split("?")[0])
            tags.append(m.group(1) if m else "")
            return url

        dest = self.cache.path("dumps", f"{kind}.parquet")
        self.client.download(sign, dest)
        return dest, tags[-1]

    @staticmethod
    def _read_kline(path, min_date: pd.Timestamp | None) -> pd.DataFrame:
        filters = [("date_ms", ">=", date_to_ms(min_date))] if min_date is not None else None
        df = pd.read_parquet(path, columns=["thscode", "date_ms", "open_price", "high_price", "low_price",
                                            "close_price", "volume", "turnover"], filters=filters)
        df = df.rename(columns={"open_price": "open", "high_price": "high", "low_price": "low",
                                "close_price": "close"})
        df["date"] = ms_to_date(df.pop("date_ms"))
        return df[KLINE_COLS]

    def _update_store(self, trading_days: list[pd.Timestamp], keep_days: int) -> pd.DataFrame:
        store_p = self.cache.path("kline_raw.parquet")
        store = pd.read_parquet(store_p) if store_p.exists() else None
        need_full = store is None or store.empty
        if not need_full:
            last = store["date"].max()
            behind = sum(1 for d in trading_days if d > last)
            need_full = behind > 7 or store["date"].nunique() < min(keep_days, 260)
        if need_full:
            log.info("下载全市场 10 年日线 dump（首次或本地落后过多）")
            path, tag = self._dump("daily-k")
            min_date = trading_days[-1] - pd.Timedelta(days=int(keep_days * 1.6))
            store = self._read_kline(path, min_date)
            path.unlink(missing_ok=True)
        else:
            path, tag = self._dump("daily-k-10d")
            inc = self._read_kline(path, None)
            store = pd.concat([store, inc], ignore_index=True)
            store = store.drop_duplicates(["thscode", "date"], keep="last")
        log.info("日线 dump 版本 %s，本地最新日期 %s", tag, store["date"].max().date())
        dates = sorted(store["date"].unique())
        if len(dates) > keep_days:
            store = store[store["date"] >= dates[-keep_days]]
        store = store.sort_values(["thscode", "date"]).reset_index(drop=True)
        store.to_parquet(store_p, index=False)
        return store

    def _snapshot_bar(self, asof: pd.Timestamp) -> pd.DataFrame | None:
        """dump 未发布当日数据时，用全市场快照拼当日 K 线（快照须代表 asof 当日完整行情）。"""
        rows, offset, ts = [], 0, None
        while True:
            data = self.client.get("/api/a-share/prices/snapshot", {"limit": 1000, "offset": offset})
            items = data.get("item") or []
            ts = ts or data.get("timestamp")
            rows += items
            if len(items) < 1000:
                break
            offset += 1000
        if not ts or not rows:
            return None
        snap_time = pd.to_datetime(ts, unit="ms", utc=True).tz_convert("Asia/Shanghai").tz_localize(None)
        if not snapshot_covers(snap_time, asof, self.trading_days()):
            log.info("快照时间 %s 不代表 %s 收盘后的完整行情，不拼当日 K 线", snap_time, asof.date())
            return None
        df = pd.DataFrame(rows)
        df = df[(df["volume"] > 0) & df["last_price"].notna()]
        return pd.DataFrame({
            "thscode": df["thscode"], "date": asof, "open": df["open_price"], "high": df["high_price"],
            "low": df["low_price"], "close": df["last_price"], "volume": df["volume"],
            "turnover": df["turnover"],
        })

    def _load_events(self) -> pd.DataFrame | None:
        try:
            path, _ = self._dump("adjustment-factors")
            ev = pd.read_parquet(path)
            return pd.DataFrame({
                "thscode": ev["thscode"], "ex_date": ms_to_date(ev["ex_date_ms"]),
                "d": ev.get("dividend_per_share"), "s": ev.get("per_share_bonus"),
                "r": ev.get("allotment_ratio"), "p": ev.get("allotment_price")})
        except (ApiError, OSError) as exc:
            log.warning("复权事件下载失败，使用未复权价格：%s", exc)
            return None

    def _patch_recent_gaps(self, asof: pd.Timestamp) -> pd.DataFrame | None:
        """dump 未更新、快照也不可用时，用腾讯逐只补出最近缺失交易日的原始日线。

        只在最近几个交易日出现缺口（当日任务被延迟/漏跑）时触发，一次最多补 3 天；
        价格与交易所原始数据一致，成交额按 量×均价 估算，会写进报告的 warnings。
        同一进程内只尝试一次，失败就等下一次运行。"""
        if self._patch_tried:
            return None
        self._patch_tried = True
        tdays = self.trading_days()
        window = [d for d in tdays if d <= asof][-5:]
        store_max = self._store["date"].max() if len(self._store) else pd.NaT
        if not window or pd.isna(store_max) or store_max < window[0]:
            return None  # 行情库整体落后太深，等 dump 全量更新，不值得逐只拉
        missing = recent_missing_days(self._store["date"], window)
        if not missing:
            return None
        days = missing[-3:]
        start, end = days[0], days[-1]
        from .free import tencent_kline, to_prefixed  # 与流通市值一样复用腾讯公开接口
        codes = list(self.universe()["thscode"])

        def one(c: str):
            df = tencent_kline(to_prefixed(c), count=20, adjust="")
            if df.empty:
                return None
            df["thscode"] = c
            return df[(df["date"] >= start) & (df["date"] <= end)]

        frames = []
        with ThreadPoolExecutor(self.max_workers) as ex:
            for df in ex.map(one, codes):
                if df is not None and not df.empty:
                    frames.append(df)
        if not frames:
            return None
        bar = pd.concat(frames, ignore_index=True)[KLINE_COLS]
        log.warning("补齐最近缺口交易日 %s（腾讯逐只，成交额按均价估算）",
                    "、".join(f"{d:%Y-%m-%d}" for d in days))
        return bar

    def price_panel(self, asof: pd.Timestamp, keep_days: int) -> PricePanel:
        notes = []
        if self._store is None:  # 同一进程内（如 backfill）只更新一次
            self._store = self._update_store(self.trading_days(), keep_days)
            self._events = self._load_events()
        store = self._store
        if store["date"].max() < asof:
            bar = self._snapshot_bar(asof)
            if bar is not None and len(bar) > 1000:
                store = pd.concat([store, bar], ignore_index=True)
                store = store.drop_duplicates(["thscode", "date"], keep="last")
                self._store = store
                notes.append(f"{asof.date()} 日线来自收盘快照（dump 尚未发布）")
        patch = self._patch_recent_gaps(asof)
        if patch is not None and not patch.empty:
            store = pd.concat([store, patch], ignore_index=True)
            store = store.drop_duplicates(["thscode", "date"], keep="last")
            self._store = store
            gap_days = "、".join(f"{d:%Y-%m-%d}" for d in sorted(patch["date"].unique()))
            notes.append(f"缺口交易日 {gap_days} 用腾讯逐只补数（成交额按均价估算，dump 更新后自动覆盖）")
        if self._events is None:
            notes.append("复权事件下载失败，本日使用未复权价格")
        store = store[store["date"] <= asof].sort_values(["thscode", "date"]).reset_index(drop=True)
        store["factor"] = forward_factors(store, self._events)
        panel = long_to_panel(store, "hithink")
        panel.notes = notes
        return panel

    # ---------- 指数 / 板块 ----------
    def index_bars(self, code: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        data = self.client.get("/api/a-share-index/prices/historical", {
            "thscode": code, "interval": "1d", "start": date_to_ms(start),
            "end": date_to_ms(end) + 86_399_000})
        df = pd.DataFrame(data.get("item") or [])
        if df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "turnover"])
        df["date"] = ms_to_date(df["date_ms"])
        df = df.rename(columns={"open_price": "open", "high_price": "high", "low_price": "low",
                                "close_price": "close"})
        return df[["date", "open", "high", "low", "close", "volume", "turnover"]].sort_values("date")

    def sector_list(self) -> pd.DataFrame:
        tag = self.cfg["sectors"].get("tag", "industry")
        data = self.client.get("/api/a-share-index/catalog/ths-index-list", {"tag": tag})
        df = pd.DataFrame(data.get("item") or []).rename(columns={"thscode": "code"})
        prefix = self.cfg["sectors"].get("code_prefix")
        if prefix:
            df = df[df["code"].str.startswith(str(prefix))]
        return df[["code", "name"]].reset_index(drop=True)

    def sector_constituents(self, code: str) -> pd.DataFrame:
        data = self.client.get("/api/a-share-index/constituents/ths-stock-list", {"thscode": code})
        df = pd.DataFrame(data.get("item") or [])
        if df.empty:
            return pd.DataFrame(columns=["thscode", "name"])
        return df[["thscode", "name"]]

    # ---------- 财报 ----------
    def financials(self, thscode: str) -> dict:
        def income(period, limit):
            data = self.client.get("/api/a-share/financials/income-statements",
                                   {"thscode": thscode, "period": period, "limit": limit})
            out = []
            for x in data.get("item") or []:
                out.append({
                    "period_end": str(ms_to_date(x["period_end_ms"]).date()),
                    "report_date": str(ms_to_date(x["report_date_ms"]).date()) if x.get("report_date_ms") else None,
                    "fiscal_year": x.get("fiscal_year"),
                    "quarter": QMAP.get(str(x.get("fiscal_period")).upper()),
                    "revenue": x.get("operating_income"),
                    "parent_np": x.get("parent_holder_net_profit"),
                    "eps": x.get("basic_eps"),
                })
            return out

        def balance():
            data = self.client.get("/api/a-share/financials/balance-sheets",
                                   {"thscode": thscode, "period": "annual", "limit": 5})
            return [{
                "period_end": str(ms_to_date(x["period_end_ms"]).date()),
                "report_date": str(ms_to_date(x["report_date_ms"]).date()) if x.get("report_date_ms") else None,
                "fiscal_year": x.get("fiscal_year"),
                "equity": x.get("holder_equity_total"),
            } for x in data.get("item") or []]

        return {"quarterly": income("quarterly", 12), "annual": income("annual", 5), "balance": balance()}

    def financials_many(self, codes: list[str]) -> dict[str, dict | None]:
        def one(c):
            try:
                return c, self.financials(c)
            except ApiError as exc:
                log.warning("财报获取失败 %s: %s", c, exc)
                return c, None
        with ThreadPoolExecutor(self.max_workers) as ex:
            return dict(ex.map(one, codes))

    # ---------- I 因子代理 / 估值 ----------
    def lhb_org(self, date: pd.Timestamp) -> pd.DataFrame | None:
        data = self.client.get("/api/a-share/special-data/dragon-tiger-list",
                               {"board_type": "org", "date": date.strftime("%Y-%m-%d")})
        empty = pd.DataFrame(columns=["thscode", "org_net_value"])
        if str(data.get("trade_date", ""))[:10] != date.strftime("%Y-%m-%d"):
            return empty
        df = pd.DataFrame(data.get("stock_items") or [])
        return empty if df.empty else df[["thscode", "org_net_value"]]

    def valuations(self, codes: list[str]) -> pd.DataFrame:
        frames = []
        codes = list(dict.fromkeys(codes))
        for i in range(0, len(codes), 100):
            try:
                data = self.client.get("/api/a-share/valuations/snapshot",
                                       {"thscodes": ",".join(codes[i:i + 100])})
                frames.append(pd.DataFrame(data.get("item") or []))
            except ApiError as exc:
                log.warning("估值快照失败: %s", exc)
        if not frames:
            return super().valuations(codes)
        df = pd.concat(frames, ignore_index=True)
        return df[[c for c in ["thscode", "pe_ttm", "pb_mrq"] if c in df]]
