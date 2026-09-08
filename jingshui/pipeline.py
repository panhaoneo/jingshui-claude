"""编排层: 把 client 取到的数据喂给 L0/L1/L2/L3。

大结果一律落盘 (契约《大结果规则》), 调用方只拿到路径和摘要。
本层唯一发网络请求, 其余模块都是纯函数, 因此可以离线测试。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Sequence

from .client import HithinkClient, HithinkError
from .indicators import Bar, to_bars, trailing_return
from .market import DEFAULT_INDEXES, MarketVerdict, judge_market
from .rules import EntrySignal, evaluate_entry
from .screener import StockInput, StockScore, rank_candidates, score_stock
from .sectors import SectorScore, leading_sectors, score_sectors

LOG = logging.getLogger(__name__)

DAY_MS = 86_400_000


def now_ms() -> int:
    return int(time.time() * 1000)


def days_ago_ms(days: int, ref_ms: int | None = None) -> int:
    return (ref_ms or now_ms()) - days * DAY_MS


def date_str(ms: int) -> str:
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d")


class Cache:
    """朴素的按 key 落盘 JSON 缓存。

    存在的理由: 板块目录、成分股和多年日线都是大结果, 重复拉取既慢又浪费配额。
    缓存里不存任何凭据。
    """

    def __init__(self, root: str, ttl_seconds: int = 12 * 3600) -> None:
        self.root = root
        self.ttl = ttl_seconds
        os.makedirs(root, exist_ok=True)

    def _path(self, key: str) -> str:
        safe = key.replace("/", "_").replace(":", "_")
        return os.path.join(self.root, f"{safe}.json")

    def get(self, key: str):
        path = self._path(key)
        if not os.path.exists(path):
            return None
        if self.ttl and (time.time() - os.path.getmtime(path)) > self.ttl:
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None

    def put(self, key: str, value) -> None:
        with open(self._path(key), "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False)


@dataclass
class ScanConfig:
    lookback_days: int = 500          # 需要 >=200 根日 K 才能算 MA200
    sector_tag: str = "industry"      # industry / cn_concept
    top_sectors: int = 5
    require_sector_new_high: bool = True
    max_stocks_per_sector: int = 40
    rs_reference_index: str | None = None  # 见 stock_scan 里对 RS 样本总体的说明
    org_flow_days: int = 60   # 龙虎榜回溯天数, 每天一次请求, 限流时可调小
    cache_dir: str = ".cache/jingshui"
    cache_ttl: int = 12 * 3600
    request_pause: float = 0.15       # 每次请求之间的最小间隔, 降低触发限流的概率


class Pipeline:
    def __init__(self, client: HithinkClient, config: ScanConfig | None = None) -> None:
        self.client = client
        self.config = config or ScanConfig()
        self.cache = Cache(self.config.cache_dir, self.config.cache_ttl)
        # 节流统一交给 client: 它能在撞到限流时自动放大间隔, 也覆盖重试请求,
        # 而 pipeline 自己 sleep 只能覆盖首次调用。
        if hasattr(client, "set_min_interval"):
            client.set_min_interval(self.config.request_pause)

    # ---------- 工具 ----------

    def _window(self) -> tuple[int, int]:
        end = now_ms()
        return days_ago_ms(self.config.lookback_days, end), end

    # ---------- L0 ----------

    def market_gate(self, indexes: dict[str, str] | None = None) -> MarketVerdict:
        indexes = indexes or DEFAULT_INDEXES
        start, end = self._window()
        bars: dict[str, Sequence[Bar]] = {}
        for code in indexes:
            key = f"idx_{code}_{self.config.lookback_days}"
            rows = self.cache.get(key)
            if rows is None:
                rows = self.client.index_history(code, start, end)
                self.cache.put(key, rows)
            bars[code] = to_bars(rows)
        return judge_market(bars, indexes)

    # ---------- L1 ----------

    def sector_scan(self) -> list[SectorScore]:
        catalog_key = f"catalog_{self.config.sector_tag}"
        catalog = self.cache.get(catalog_key)
        if catalog is None:
            catalog = self.client.index_catalog(self.config.sector_tag)
            self.cache.put(catalog_key, catalog)

        names = {row["thscode"]: row.get("name", row["thscode"]) for row in catalog}
        start, end = self._window()
        bars: dict[str, Sequence[Bar]] = {}
        for code in names:
            key = f"idx_{code}_{self.config.lookback_days}"
            rows = self.cache.get(key)
            if rows is None:
                try:
                    rows = self.client.index_history(code, start, end)
                except HithinkError as exc:
                    LOG.warning("板块 %s 行情不可用: %s", code, exc)
                    continue
                self.cache.put(key, rows)
            if rows:
                bars[code] = to_bars(rows)
        return score_sectors(bars, names)

    def leading(self, scores: Sequence[SectorScore] | None = None) -> list[SectorScore]:
        scores = scores if scores is not None else self.sector_scan()
        return leading_sectors(
            scores,
            top_n=self.config.top_sectors,
            require_new_high=self.config.require_sector_new_high,
        )

    # ---------- L2 ----------

    def _stock_bars(self, thscode: str) -> list[Bar]:
        key = f"px_{thscode}_{self.config.lookback_days}"
        rows = self.cache.get(key)
        if rows is None:
            start, end = self._window()
            rows = self.client.price_history(thscode, start, end, adjust="forward")
            self.cache.put(key, rows)
        return to_bars(rows)

    def _financials(self, thscode: str) -> tuple[list[dict], list[dict], list[dict]]:
        key = f"fin_{thscode}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached["quarterly"], cached["annual"], cached["balance"]
        quarterly = self.client.income_statements(thscode, "quarterly", 12)
        annual = self.client.income_statements(thscode, "annual", 5)
        balance = self.client.balance_sheets(thscode, "annual", 5)
        self.cache.put(
            key, {"quarterly": quarterly, "annual": annual, "balance": balance}
        )
        return quarterly, annual, balance

    def org_flows(self, days: int = 60) -> dict[str, float]:
        """汇总近 days 个自然日的龙虎榜机构净额, 作为 I 因子的代理变量。

        只能覆盖上榜个股; 未上榜不等于没有机构参与, 见 screener.score_i 的说明。
        """
        totals: dict[str, float] = {}
        end = now_ms()
        for i in range(days):
            day = date_str(end - i * DAY_MS)
            key = f"dt_{day}"
            payload = self.cache.get(key)
            if payload is None:
                try:
                    payload = self.client.dragon_tiger("org", day)
                except HithinkError as exc:
                    # 非交易日或超出一年窗口, 跳过即可
                    LOG.debug("龙虎榜 %s 不可用: %s", day, exc)
                    payload = {}
                self.cache.put(key, payload)
            for row in (payload or {}).get("stock_items") or []:
                code = row.get("thscode")
                val = row.get("org_net_value")
                if code and val is not None:
                    totals[code] = totals.get(code, 0.0) + float(val)
        return totals

    def stock_scan(
        self,
        sectors: Sequence[SectorScore],
        org_net: dict[str, float] | None = None,
    ) -> tuple[list[StockScore], dict[str, str]]:
        """在主线板块成分股中跑六因子。返回 (评分列表, 代码->板块名)。

        关于 RS 样本总体的一个重要约束: 欧奈尔的 RS 评级是相对**全市场**排名,
        但把全部 A 股的日线都拉一遍代价过高。默认实现只用主线板块成分股构成
        样本总体, 这是一个偏强的池子, 会让 RS >= 80 的门槛比原书更苛刻
        (方向上偏保守, 不会放进弱者, 但可能漏掉真正的领军股)。
        需要更接近原义时, 通过 ScanConfig.rs_reference_index 指定一个宽基指数
        (如 000300.SH), 把它的成分股一并纳入样本总体。
        """
        org_net = org_net if org_net is not None else {}
        sector_of: dict[str, str] = {}
        members: dict[str, list[str]] = {}

        for sec in sectors:
            key = f"cons_{sec.thscode}"
            rows = self.cache.get(key)
            if rows is None:
                rows = self.client.index_constituents(sec.thscode)
                self.cache.put(key, rows)
            codes = [r["thscode"] for r in rows][: self.config.max_stocks_per_sector]
            members[sec.thscode] = codes
            for c in codes:
                sector_of.setdefault(c, sec.name)

        # 先把所有 K 线取回来, 用于构造 RS 排名的样本总体
        bars_by_code: dict[str, list[Bar]] = {}
        for code in sector_of:
            try:
                bars_by_code[code] = self._stock_bars(code)
            except HithinkError as exc:
                LOG.warning("%s 行情不可用: %s", code, exc)

        returns_250 = {
            c: trailing_return(b, 250) for c, b in bars_by_code.items()
        }
        market_pop = [v for v in returns_250.values() if v is not None]

        # 可选: 用一个宽基指数的成分股扩大 RS 样本总体, 更接近"全市场排名"的原义
        ref = self.config.rs_reference_index
        if ref:
            ref_key = f"cons_{ref}"
            ref_rows = self.cache.get(ref_key)
            if ref_rows is None:
                ref_rows = self.client.index_constituents(ref)
                self.cache.put(ref_key, ref_rows)
            for row in ref_rows:
                code = row["thscode"]
                if code in returns_250:
                    continue
                try:
                    ret = trailing_return(self._stock_bars(code), 250)
                except HithinkError as exc:
                    LOG.warning("RS 参考成分 %s 行情不可用: %s", code, exc)
                    continue
                if ret is not None:
                    market_pop.append(ret)

        scores: list[StockScore] = []
        for sec in sectors:
            peer_pop = [
                returns_250[c]
                for c in members.get(sec.thscode, [])
                if returns_250.get(c) is not None
            ]
            for code in members.get(sec.thscode, []):
                bars = bars_by_code.get(code)
                if not bars:
                    continue
                try:
                    quarterly, annual, balance = self._financials(code)
                except HithinkError as exc:
                    LOG.warning("%s 财务数据不可用: %s", code, exc)
                    continue
                scores.append(
                    score_stock(
                        StockInput(
                            thscode=code,
                            name=code,
                            bars=bars,
                            quarterly_income=quarterly,
                            annual_income=annual,
                            annual_balance=balance,
                            market_returns_250d=market_pop,
                            sector_returns_250d=peer_pop,
                            org_net_value_60d=org_net.get(code),
                        )
                    )
                )
        return scores, sector_of

    # ---------- L3 ----------

    def entry_signals(
        self, candidates: Sequence[StockScore], verdict: MarketVerdict
    ) -> list[EntrySignal]:
        out: list[EntrySignal] = []
        for cand in candidates:
            bars = self._stock_bars(cand.thscode)
            above50 = None
            if len(bars) >= 50:
                ma50 = sum(b.close for b in bars[-50:]) / 50
                above50 = bars[-1].close > ma50
            out.append(evaluate_entry(cand.thscode, bars, verdict.state, above50))
        return out

    # ---------- 全流程 ----------

    def run(self, out_dir: str = "output") -> dict:
        """跑完 L0->L3, 结果落盘, 返回摘要。"""
        os.makedirs(out_dir, exist_ok=True)
        verdict = self.market_gate()
        summary: dict = {
            "generated_at": date_str(now_ms()),
            "market": {
                "state": verdict.state.value,
                "max_exposure": verdict.max_exposure,
                "stop_loss_pct": verdict.stop_loss_pct,
                "per_index": [asdict(v) | {"state": v.state.value} for v in verdict.per_index],
            },
        }

        if verdict.state.value == "DEFENSE":
            summary["note"] = "大盘处于 DEFENSE, 不做选股, 最优操作是空仓或只留强势票"
            _dump(os.path.join(out_dir, "summary.json"), summary)
            return summary

        all_sectors = self.sector_scan()
        leaders = self.leading(all_sectors)
        _dump(os.path.join(out_dir, "sectors.json"), [asdict(s) for s in all_sectors])
        summary["leading_sectors"] = [
            {"thscode": s.thscode, "name": s.name, "score": s.score} for s in leaders
        ]

        if not leaders:
            summary["note"] = "没有板块指数处于新高附近, 当前无主线, 不开新仓"
            _dump(os.path.join(out_dir, "summary.json"), summary)
            return summary

        org_net = self.org_flows(self.config.org_flow_days)
        scores, sector_of = self.stock_scan(leaders, org_net)
        candidates = rank_candidates(scores)
        _dump(
            os.path.join(out_dir, "scores.json"),
            [_score_to_dict(s, sector_of.get(s.thscode)) for s in scores],
        )

        signals = self.entry_signals(candidates, verdict)
        _dump(os.path.join(out_dir, "signals.json"), [asdict(s) | {"action": s.action.value} for s in signals])

        summary["candidate_count"] = len(candidates)
        summary["buy_now"] = [s.thscode for s in signals if s.action.value == "BUY"]
        summary["watch"] = [s.thscode for s in signals if s.action.value == "WATCH"]
        summary["files"] = {
            "sectors": os.path.join(out_dir, "sectors.json"),
            "scores": os.path.join(out_dir, "scores.json"),
            "signals": os.path.join(out_dir, "signals.json"),
        }
        _dump(os.path.join(out_dir, "summary.json"), summary)
        return summary


def _score_to_dict(s: StockScore, sector: str | None) -> dict:
    return {
        "thscode": s.thscode,
        "name": s.name,
        "sector": sector,
        "total": s.total,
        "passed": s.passed,
        "excluded": s.excluded,
        "exclusion_reasons": s.exclusion_reasons,
        "factors": {
            k: {"score": round(v.score, 2), "max": v.max_score, "reasons": v.reasons}
            for k, v in s.factors.items()
        },
        "metrics": s.metrics,
    }


def _dump(path: str, payload) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
