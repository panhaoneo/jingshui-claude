"""每日执行清单（docs/03 §3）：
1. L0 → M 状态与仓位上限；2. L1 → 前 5 主线板块；3. L2 → 主线成分股六因子评分；
4. L3 → 买点三条件，满足进"可下单"，否则进"观察"；5. 持仓体检；6. 记录。

另外输出一份"全市场领军（非主线）"研究池：不属于主线板块、但 RS ≥90 且接近新高的股票，
同样打分，仅供研究，不进入可下单列表（框架规定 L2 只在主线板块内选股）。
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yaml

from . import __version__
from .cache import Cache
from .client import ApiError
from .fundamentals import a_metrics, c_metrics
from .market import market_gate
from .providers import make_provider
from .scoring import hard_exclusions, score_stock
from .sectors import score_sectors
from .signals import buy_point, check_holding
from .technicals import market_metrics, rs_history

log = logging.getLogger(__name__)
BJ = ZoneInfo("Asia/Shanghai")


class NotReady(RuntimeError):
    pass


def load_config(path: str | Path = "config.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def clean(o):
    """JSON 序列化：NaN/inf → None，numpy/pandas 标量 → Python 原生。"""
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return None if (math.isnan(o) or math.isinf(o)) else round(float(o), 6)
    if isinstance(o, pd.Timestamp):
        return o.strftime("%Y-%m-%d")
    return o


def resolve_asof(tdays: list[pd.Timestamp], asof: str | None) -> pd.Timestamp:
    now = datetime.now(BJ)
    today = pd.Timestamp(now.date())
    if asof:
        d = pd.Timestamp(asof)
        if d not in set(tdays):
            raise ValueError(f"{asof} 不是交易日")
        return d
    past = [d for d in tdays if d <= today]
    if past[-1] == today and (now.hour, now.minute) < (15, 30):
        return past[-2]
    return past[-1]


class Runner:
    def __init__(self, cfg: dict, provider: str | None = None, portfolio_path: str = "portfolio.yaml"):
        self.cfg = cfg
        self.cache = Cache(cfg["data"]["cache_dir"])
        self.pname = provider or cfg["data"]["provider"]
        self.p = make_provider(self.pname, cfg, self.cache)
        self.out_dir = Path(cfg["data"]["output_dir"])
        self.portfolio_path = Path(portfolio_path)
        self.warnings: list[str] = []
        self.fetch_end: pd.Timestamp | None = None
        self._idx_mem: dict[str, pd.DataFrame] = {}

    def backfill(self, days: int) -> list[dict]:
        """按时间顺序补跑最近 N 个交易日（时点纪律：每一天只用当天已知的数据）。
        已有结果的日期跳过。注意：成分股只有当前口径，补跑结果有幸存者偏差。"""
        tdays = self.p.trading_days()
        latest = resolve_asof(tdays, None)
        self.fetch_end = latest
        todo = [d for d in tdays if d <= latest][-days:]
        out = []
        for i, d in enumerate(todo):
            try:  # 最后一天强制重跑：让 latest.json 带上完整的信号追踪
                out.append(self.run(f"{d:%Y-%m-%d}", force=i == len(todo) - 1))
            except NotReady as exc:
                log.warning("%s：%s", d.date(), exc)
        return out

    # ---------- 缓存封装 ----------
    def index_frame(self, code: str, asof: pd.Timestamp, days: int = 900) -> pd.DataFrame:
        """指数/板块日线：本地缓存 + 增量更新；同一进程内只请求一次（backfill 复用）。"""
        if code not in self._idx_mem:
            end = max(self.fetch_end or asof, asof)
            p = self.cache.path("index", self.pname, f"{code}.parquet")
            old = pd.read_parquet(p) if p.exists() else None
            start = end - pd.Timedelta(days=days)
            if old is not None and len(old) and old["date"].min() <= start + pd.Timedelta(days=10):
                start = old["date"].max() - pd.Timedelta(days=10)
            new = self.p.index_bars(code, start, end)
            df = new if old is None else pd.concat([old, new]).drop_duplicates("date", keep="last")
            df = df[df["date"] >= end - pd.Timedelta(days=days)].sort_values("date").reset_index(drop=True)
            df.to_parquet(p, index=False)
            self._idx_mem[code] = df
        df = self._idx_mem[code]
        return df[df["date"] <= asof].reset_index(drop=True)

    def constituents(self, code: str) -> list[dict]:
        ttl = self.cfg["data"]["constituents_cache_days"]
        return self.cache.json(f"cons/{self.pname}/{code}", ttl,
                               lambda: self.p.sector_constituents(code).to_dict("records"))

    def financials(self, codes: list[str], asof: pd.Timestamp) -> dict[str, dict | None]:
        d = self.cfg["data"]
        ttl = d["fin_cache_days_season"] if asof.month in (4, 8, 10) else d["fin_cache_days"]
        out, missing = {}, []
        for c in codes:
            p = self.cache.path("fin", self.pname, f"{c}.json")
            if p.exists() and self.cache.age_days(p) < ttl:
                out[c] = json.loads(p.read_text(encoding="utf-8"))
            else:
                missing.append(c)
        if missing:
            log.info("拉取财报 %d 只（缓存命中 %d 只）", len(missing), len(out))
            got = self.p.financials_many(missing)
            for c, fin in got.items():
                if fin is not None:
                    self.cache.write_json(self.cache.path("fin", self.pname, f"{c}.json"), fin)
                else:  # 拉取失败时退回旧缓存（过期总比没有好，但标注）
                    p = self.cache.path("fin", self.pname, f"{c}.json")
                    fin = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
                out[c] = fin
        return out

    def lhb_org_sum(self, tdays: list[pd.Timestamp], asof: pd.Timestamp, n: int) -> pd.Series | None:
        if not self.p.supports_org_flow:
            return None
        days = [d for d in tdays if d <= asof][-n:]
        frames = []
        for d in days:
            p = self.cache.path("lhb", f"{d:%Y%m%d}.parquet")
            if p.exists():
                frames.append(pd.read_parquet(p))
                continue
            try:
                df = self.p.lhb_org(d)
            except ApiError as exc:
                log.warning("龙虎榜 %s 获取失败: %s", d.date(), exc)
                continue
            if df is None:
                continue
            if len(df) or d < asof:  # 当日空结果可能是尚未发布，不缓存
                df.to_parquet(p, index=False)
            frames.append(df)
        if not frames:
            return pd.Series(dtype=float)
        all_ = pd.concat(frames, ignore_index=True)
        all_["org_net_value"] = pd.to_numeric(all_["org_net_value"], errors="coerce")
        return all_.groupby("thscode")["org_net_value"].sum()

    # ---------- 主流程 ----------
    def run(self, asof: str | None = None, force: bool = False, allow_stale: bool = False) -> dict:
        t0 = time.time()
        cfg = self.cfg
        self.warnings = []
        tdays = self.p.trading_days()
        asof_d = resolve_asof(tdays, asof)
        daily_p = self.out_dir / "daily" / f"{asof_d:%Y-%m-%d}.json"
        if daily_p.exists() and not force:
            log.info("%s 已有结果，跳过（--force 重跑）", asof_d.date())
            return {"status": "skipped", "date": f"{asof_d:%Y-%m-%d}"}

        # ---- 数据：全市场日线 ----
        panel = self.p.price_panel(asof_d, cfg["data"]["keep_trading_days"])
        self.warnings += panel.notes
        if panel.asof < asof_d:
            msg = f"日线最新日期 {panel.asof.date()} 早于目标交易日 {asof_d.date()}，数据尚未就绪"
            if not allow_stale:
                raise NotReady(msg)
            self.warnings.append(msg + "（--allow-stale：按最新可用日期运行）")
            asof_d = panel.asof
            daily_p = self.out_dir / "daily" / f"{asof_d:%Y-%m-%d}.json"
        uni = self.p.universe()
        names = dict(zip(uni["thscode"], uni["name"]))

        # ---- L0 ----
        mcfg = cfg["market"]
        idx_frames = {x["code"]: self.index_frame(x["code"], asof_d) for x in mcfg["indices"]}
        market = market_gate(idx_frames, {x["code"]: x["name"] for x in mcfg["indices"]}, mcfg)
        log.info("L0 M 状态：%s（%s）", market["state"], market["reason"])

        # ---- L1 ----
        scfg = cfg["sectors"]
        slist = self.p.sector_list()
        snames = dict(zip(slist["code"], slist["name"]))
        sframes = {}
        for code in slist["code"]:
            try:
                sframes[code] = self.index_frame(code, asof_d)
            except ApiError as exc:
                log.warning("板块 %s 日线失败: %s", code, exc)
        sectors = score_sectors(sframes, snames, scfg)
        mainline = sectors[sectors["mainline"]]
        log.info("L1 主线：%s", "、".join(mainline["name"]))

        stock_sector: dict[str, str] = {}
        sector_members: dict[str, list[str]] = {}
        for code in slist["code"]:
            try:
                mem = [m["thscode"] for m in self.constituents(code)]
            except ApiError as exc:
                log.warning("成分股 %s 失败: %s", code, exc)
                mem = []
            sector_members[code] = mem
            for m in mem:
                stock_sector.setdefault(m, code)

        # ---- 全市场技术面 ----
        stc = cfg["stocks"]
        tech = market_metrics(panel, stc)
        tech["name"] = [names.get(c, "") for c in tech.index]
        tech["turnover_pct"] = tech["avg_turnover20"].rank(pct=True) * 100
        tech["sector"] = [stock_sector.get(c) for c in tech.index]
        tech["sector_name"] = [snames.get(s, "") if s else "" for s in tech["sector"]]
        # 板块内 250 日涨幅排名（1=最强），用于 L 因子"板块内前 20%"
        tech["sector_rank_pct"] = np.nan
        for code, mem in sector_members.items():
            mem = [m for m in mem if m in tech.index]
            if len(mem) < 3:
                continue
            r = tech.loc[mem, "ret250"].rank(ascending=False, pct=True)
            tech.loc[mem, "sector_rank_pct"] = r
        tech = tech[tech["close"].notna()]

        # ---- 候选池：主线成分股 + 非主线领军研究池 ----
        main_codes = set()
        for code in mainline["code"]:
            main_codes.update(m for m in sector_members.get(code, []) if m in tech.index)
        excl_counts: dict[str, int] = {}
        pool_main, pool_research, tech_fail = [], [], []
        for code, row in tech.iterrows():
            in_main = code in main_codes
            reasons = hard_exclusions(row, row["name"], stc)
            if reasons:
                if in_main:
                    for r in reasons:
                        excl_counts[r] = excl_counts.get(r, 0) + 1
                continue
            tech_ok = (row["rs"] >= stc["L"]["rs_start"]) and (row["dist_high"] <= stc["N"]["half_dist"])
            if in_main:
                if tech_ok:
                    pool_main.append(code)
                else:
                    tech_fail.append(code)
            elif row["rs"] >= stc["research_min_rs"] and row["dist_high"] <= stc["N"]["half_dist"]:
                pool_research.append(code)
        if tech_fail:
            excl_counts["RS<80 或距高点>10%（L/N 因子为 0）"] = len(tech_fail)
        pool_research = sorted(pool_research, key=lambda c: -tech.at[c, "rs"])[: stc["research_top_n"]]
        log.info("L2 主线候选 %d 只，非主线研究池 %d 只", len(pool_main), len(pool_research))

        codes = pool_main + pool_research
        fins = self.financials(codes, asof_d)
        org = self.lhb_org_sum(tdays, asof_d, stc["I"]["lhb_days"])
        if org is None:
            self.warnings.append("当前数据源无龙虎榜机构净额，I 因子仅含成交额部分")
        mcap = self.p.float_mcap(codes) if codes else {}
        if codes and not mcap:
            self.warnings.append("流通市值获取失败，S 因子市值部分按中性分处理")

        scored = []
        for code in codes:
            row = tech.loc[code].to_dict()
            fin = fins.get(code)
            c = c_metrics(fin, asof_d) if fin else {"available": False}
            a = a_metrics(fin, asof_d, stc["A"]["years"]) if fin else {"available": False}
            if fin is None:
                excl_counts["财报缺失"] = excl_counts.get("财报缺失", 0) + (code in pool_main)
                continue
            if c.get("cum_np") is not None and c["cum_np"] < 0:
                if code in pool_main:
                    excl_counts["最近一期归母净利润为负"] = excl_counts.get("最近一期归母净利润为负", 0) + 1
                continue
            row["org_net_60d"] = None if org is None else float(org.get(code, 0.0))
            row["float_mcap_yi"] = mcap.get(code)
            sc = score_stock(row, c, a, stc, org is not None)
            scored.append({
                "code": code, "name": row["name"], "sector": row["sector_name"],
                "mainline": code in main_codes, "tech": row, "fin_c": c, "fin_a": a, **sc,
            })

        # ---- L3 ----
        b = cfg["buy"]
        for s in scored:
            if not s["passed"]:
                continue
            df = panel.series(s["code"])
            bp = buy_point(df, b)
            if bp.get("ok") and market["state"] == "DEFENSE":
                bp["stops"] = [round(bp["entry"] * (1 - b["stop_loss_defense"]), 3)]
            s["l3"] = bp

        # ---- 估值（仅事后记录） ----
        shown = [s["code"] for s in scored if s["passed"] or s["total"] >= stc["pass_score"] - 15]
        if shown:
            try:
                val = self.p.valuations(shown).set_index("thscode")
                for s in scored:
                    if s["code"] in val.index:
                        s["valuation"] = val.loc[s["code"]].to_dict()
            except Exception as exc:
                log.warning("估值快照失败: %s", exc)

        # ---- 持仓体检 ----
        portfolio = self.check_portfolio(panel, tech, market["state"], asof_d)

        # ---- 输出 ----
        result = self.build_output(asof_d, panel, market, sectors, scored, excl_counts,
                                   len(tech), len(main_codes), portfolio, t0)
        self.write(asof_d, result, panel, scored, idx_frames)
        return {"status": "ok", "date": f"{asof_d:%Y-%m-%d}",
                "buy": len(result["buy_signals"]), "candidates": len(result["candidates"])}

    # ---------- 持仓体检 ----------
    def check_portfolio(self, panel, tech, m_state, asof_d) -> list[dict]:
        if not self.portfolio_path.exists():
            return []
        holdings = (yaml.safe_load(self.portfolio_path.read_text(encoding="utf-8")) or {}).get("holdings") or []
        if not holdings:
            return []
        rs_h = rs_history(panel.close.ffill(), self.cfg["stocks"]["rs_days"])
        codes = [h["code"] for h in holdings if h.get("code") in panel.close.columns]
        fins = self.financials(codes, asof_d)
        out = []
        for h in holdings:
            code = h.get("code")
            if code not in panel.close.columns:
                out.append({"code": code, "verdict": "数据缺失", "actions": [], "notes": ["代码不在行情库中"]})
                continue
            df = panel.series(code)
            factor = (panel.close[code] / panel.raw_close[code]).dropna()
            adj = dict(h)
            adj["batches"] = []
            for bt in h.get("batches") or []:  # 买入价换算到前复权口径
                d = pd.Timestamp(bt["date"])
                f = factor[factor.index <= d]
                adj["batches"].append({**bt, "date": str(bt["date"]),
                                       "price": bt["price"] * (float(f.iloc[-1]) if len(f) else 1.0)})
            fin = fins.get(code)
            r = check_holding(code, adj, df, rs_h.get(code), c_metrics(fin, asof_d) if fin else None,
                              m_state, self.cfg)
            r["name"] = tech.at[code, "name"] if code in tech.index else ""
            out.append(r)
        return out

    # ---------- 信号追踪 ----------
    def track_signals(self, panel, asof_d) -> tuple[list[dict], dict[str, int]]:
        daily_dir = self.out_dir / "daily"
        tracked, streak = [], {}
        if not daily_dir.exists():
            return tracked, streak
        files = sorted(daily_dir.glob("*.json"))[-250:]
        recent_days = {f.stem for f in files[-20:]}
        for f in files:
            if f.stem >= f"{asof_d:%Y-%m-%d}":
                continue
            try:
                day = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if f.stem in recent_days:
                for c in day.get("candidates", []):
                    streak[c["code"]] = streak.get(c["code"], 0) + 1
            for s in day.get("buy_signals", []):
                code, d0 = s["code"], pd.Timestamp(s.get("signal_date") or f.stem)
                if code not in panel.close.columns or d0 not in panel.close.index:
                    continue
                entry = panel.close.at[d0, code]
                after_c = panel.close[code][panel.close.index > d0].dropna()
                after_l = panel.low[code][panel.low.index > d0].dropna()
                after_h = panel.high[code][panel.high.index > d0].dropna()
                if pd.isna(entry) or entry <= 0:
                    continue
                stop_px = entry * (1 - self.cfg["buy"]["stop_loss"][-1])
                hit = after_l[after_l <= stop_px]
                last = after_c.iloc[-1] if len(after_c) else entry
                tracked.append({
                    "code": code, "name": s.get("name"), "sector": s.get("sector"),
                    "signal_date": f"{d0:%Y-%m-%d}", "kind": s.get("kind"), "m_state": day["market"]["state"],
                    "days": int(len(after_c)),
                    "ret": last / entry - 1,
                    "max_gain": (after_h.max() / entry - 1) if len(after_h) else 0.0,
                    "max_dd": (after_l.min() / entry - 1) if len(after_l) else 0.0,
                    "stopped": bool(len(hit)), "stop_date": f"{hit.index[0]:%Y-%m-%d}" if len(hit) else None,
                    "ret_at_stop": (stop_px / entry - 1) if len(hit) else None,
                })
        # 同一只股票同一信号可能在 T、T+1、T+2 重复出现，只保留最早一条
        seen, dedup = set(), []
        for t in sorted(tracked, key=lambda x: x["signal_date"]):
            key = t["code"]
            prev = [d for (c, d) in seen if c == key]
            if prev and (pd.Timestamp(t["signal_date"]) - pd.Timestamp(max(prev))).days <= 7:
                continue
            seen.add((key, t["signal_date"]))
            dedup.append(t)
        return sorted(dedup, key=lambda x: x["signal_date"], reverse=True), streak

    # ---------- 组装 ----------
    def build_output(self, asof_d, panel, market, sectors, scored, excl_counts, n_universe, n_main,
                     portfolio, t0) -> dict:
        cfg = self.cfg
        tracked, streak = self.track_signals(panel, asof_d)
        cap = market["position_cap"]
        pf = cfg["portfolio"]
        target = min(pf["max_single"], cap / pf["holdings"][1]) if cap else 0

        def pack(s: dict) -> dict:
            t = s["tech"]
            return {
                "code": s["code"], "name": s["name"], "sector": s["sector"], "mainline": s["mainline"],
                "total": s["total"], "factors": s["factors"], "passed": s["passed"],
                "zero_factors": s["zero_factors"], "details": s["details"],
                "close": t["raw_close"], "chg_pct": t["chg_pct"], "rs": t["rs"], "dist_high": t["dist_high"],
                "ret250": t["ret250"], "ret60": t["ret60"], "ret20": t["ret20"],
                "avg_turnover20": t["avg_turnover20"], "vol_ratio": t["vol_ratio"],
                "ma50": t["ma50"], "ma200": t["ma200"],
                "c_history": (s["fin_c"] or {}).get("history", []),
                "a_growth": (s["fin_a"] or {}).get("growth", []), "roe": (s["fin_a"] or {}).get("roe"),
                "report_period": (s["fin_c"] or {}).get("period"),
                "report_date": (s["fin_c"] or {}).get("report_date"),
                "l3": s.get("l3"), "valuation": s.get("valuation"),
                "streak": streak.get(s["code"], 0),
            }

        passed = [s for s in scored if s["passed"] and s["mainline"]]
        buys = []
        for s in passed:
            l3 = s.get("l3") or {}
            if l3.get("ok"):
                entry = pack(s)
                entry.update({
                    "kind": l3.get("kind"), "signal_date": l3.get("signal_date"), "entry": l3.get("entry"),
                    "stops": l3.get("stops"), "actionable": market["allow_new"],
                    "position": {"target": target, "batches": [round(target * x, 4) for x in cfg["buy"]["batches"]]},
                })
                buys.append(entry)
        buys.sort(key=lambda x: -x["total"])
        cands = sorted([pack(s) for s in passed], key=lambda x: -x["total"])
        near = sorted([pack(s) for s in scored if s["mainline"] and not s["passed"]], key=lambda x: -x["total"])[:40]
        research = sorted([pack(s) for s in scored if not s["mainline"]], key=lambda x: -x["total"])

        sec_out = []
        for r in sectors.itertuples():
            sec_out.append({
                "code": r.code, "name": r.name, "rank": r.rank, "score": r.score, "mainline": r.mainline,
                "eligible": r.eligible, "rs_pct": r.rs_pct, "mom_pct": r.mom_pct, "high_score": r.high_score,
                "dist_high": r.dist_high, "trend_ok": r.trend_ok, "ret_rs": r.ret_rs, "ret_mom": r.ret_mom,
                "ret_20": r.ret_20, "chg_pct": r.chg_pct, "close": r.close,
            })
        def summarize(items: list[dict]) -> dict:
            rets = [t["ret_at_stop"] if t["stopped"] else t["ret"] for t in items]
            return {
                "signals": len(items),
                "win_rate": sum(1 for r in rets if r > 0) / len(rets) if rets else None,
                "avg_ret": float(np.mean(rets)) if rets else None,
                "stopped": sum(1 for t in items if t["stopped"]),
            }

        stats = summarize(tracked)
        stats["by_state"] = {st: summarize([t for t in tracked if t["m_state"] == st])
                             for st in ("OFFENSE", "CAUTION", "DEFENSE")}
        return {
            "meta": {
                "date": f"{asof_d:%Y-%m-%d}", "generated_at": datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S"),
                "provider": self.pname, "version": __version__, "elapsed_s": round(time.time() - t0, 1),
                "api_calls": getattr(getattr(self.p, "client", None), "calls", None),
                "warnings": self.warnings,
            },
            "market": market,
            "sectors": sec_out,
            "funnel": {
                "universe": n_universe, "mainline_members": n_main,
                "scored": sum(1 for s in scored if s["mainline"]), "passed": len(passed), "buy": len(buys),
                "excluded": excl_counts,
            },
            "buy_signals": buys,
            "candidates": cands,
            "near_miss": near,
            "research": research,
            "portfolio": portfolio,
            "tracking": {"stats": stats, "items": tracked[:200]},
            "config": {k: cfg[k] for k in ("market", "sectors", "stocks", "buy", "portfolio", "sell")},
        }

    def write(self, asof_d, result, panel, scored, idx_frames) -> None:
        out = self.out_dir
        (out / "daily").mkdir(parents=True, exist_ok=True)
        data = clean(result)
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        (out / "daily" / f"{asof_d:%Y-%m-%d}.json").write_text(text, encoding="utf-8")
        newest = max((p.stem for p in (out / "daily").glob("*.json")), default="")
        is_latest = f"{asof_d:%Y-%m-%d}" >= newest
        if is_latest:  # 补跑旧日期时不覆盖最新结果
            (out / "latest.json").write_text(text, encoding="utf-8")

        # 图表数据只保留最新一份（不进每日归档，避免仓库膨胀）
        charts = {}
        codes = {s["code"] for s in scored if s["passed"] or s["total"] >= self.cfg["stocks"]["pass_score"] - 15}
        codes |= {h["code"] for h in result["portfolio"] if h.get("code") in panel.close.columns}
        codes |= {t["code"] for t in result["tracking"]["items"][:60] if t["code"] in panel.close.columns}
        for code in codes:
            df = panel.series(code).tail(400)
            charts[code] = {
                "d": [x.strftime("%Y%m%d") for x in df.index],
                "o": [round(float(x), 3) for x in df["open"]], "h": [round(float(x), 3) for x in df["high"]],
                "l": [round(float(x), 3) for x in df["low"]], "c": [round(float(x), 3) for x in df["close"]],
                "v": [int(x) if not pd.isna(x) else 0 for x in df["volume"]],
            }
        if is_latest:
            (out / "charts.json").write_text(json.dumps(clean(charts), separators=(",", ":")), encoding="utf-8")

        idx_p = out / "index.json"
        index = json.loads(idx_p.read_text(encoding="utf-8")) if idx_p.exists() else {"days": []}
        days = [d for d in index["days"] if d["date"] != data["meta"]["date"]]
        days.append({
            "date": data["meta"]["date"], "m_state": data["market"]["state"],
            "buy": len(data["buy_signals"]), "candidates": len(data["candidates"]),
            "mainline": [s["name"] for s in data["sectors"] if s["mainline"]],
        })
        index["days"] = sorted(days, key=lambda d: d["date"], reverse=True)
        index["updated"] = data["meta"]["generated_at"]
        idx_p.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("输出：%s（买点 %d，候选 %d）", out / "latest.json", len(data["buy_signals"]), len(data["candidates"]))
