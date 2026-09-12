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

import pandas as pd

from ..adjust import forward_factors
from ..client import ApiError, HiThinkClient
from ..models import PricePanel, date_to_ms, long_to_panel, ms_to_date
from .base import Provider

log = logging.getLogger(__name__)

KLINE_COLS = ["thscode", "date", "open", "high", "low", "close", "volume", "turnover"]
QMAP = {"Q1": 1, "Q2": 2, "H1": 2, "Q3": 3, "Q4": 4, "FY": 4}


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
        """dump 未发布当日数据时，用全市场快照拼当日 K 线（快照时间须在 asof 当日 15:00 之后）。"""
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
        if snap_time.normalize() != asof or snap_time.hour < 15:
            log.info("快照时间 %s 不是 %s 收盘后，不拼当日 K 线", snap_time, asof.date())
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
