"""L2 个股六因子（CAN SLIM 的 A 股版）与硬性排除。

| 因子 | 指标                                   | 记分线                     | 权重 |
|------|----------------------------------------|----------------------------|------|
| C    | 单季归母净利同比 / 营收同比 / 加速     | ≥25% 起分，≥50% 满分       | 20   |
| A    | 近 3 年年度净利增速 / ROE              | 每年 ≥25%；ROE ≥17% 起分   | 15   |
| N    | 距 250 日最高价                        | ≤5% 满分，≤10% 半分        | 20   |
| S    | 起涨日量比 / 流通市值                  | ≥1.4 倍 20 日均量          | 10   |
| L    | 250 日涨幅全市场百分位 / 板块内排名    | ≥80 起分，≥90 满分         | 20   |
| I    | 近 60 日龙虎榜机构净买入 / 日均成交额  | >0                         | 10   |

入选线：总分 ≥70 且 C、A、N、L 均不为 0。
注：框架原文的六项权重合计为 95 分（M 在 L0 作闸门，不计分），本实现保留原权重不做放大。
"""
from __future__ import annotations

import math

import pandas as pd


def _lin(x, x0, x1, y0, y1):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    if x <= x0:
        return y0
    if x >= x1:
        return y1
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def _num(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def hard_exclusions(t: pd.Series, name: str, scfg: dict) -> list[str]:
    reasons = []
    if "ST" in name.upper() or "退" in name:
        reasons.append("ST/退市风险")
    if t["bars"] < scfg["min_bars"]:
        reasons.append(f"上市不足 {scfg['min_bars']} 个交易日")
    if not (t["avg_turnover20"] >= scfg["min_avg_turnover"]):
        reasons.append("20 日均成交额 < 5000 万")
    if not (t["dist_high"] <= scfg["max_dist_high"]):
        reasons.append("距 250 日高点 > 30%")
    return reasons


def score_c(c: dict, cfg: dict) -> tuple[float, dict]:
    w = cfg["weight"]
    if not c.get("available"):
        return 0.0, {"note": "无季度财报"}
    g, rev, prev = _num(c.get("np_yoy")), _num(c.get("rev_yoy")), _num(c.get("prev_np_yoy"))
    profit_w, rev_w, acc_w = w * 0.7, w * 0.2, w * 0.1
    profit = 0.0
    if g is not None and g >= cfg["profit_start"]:
        profit = _lin(g, cfg["profit_start"], cfg["profit_full"], profit_w / 2, profit_w)
    revenue = _lin(rev, cfg["revenue_min"] - 0.15, cfg["revenue_min"], 0, rev_w) if rev is not None else 0.0
    accel = acc_w if (g is not None and prev is not None and g > prev and profit > 0) else 0.0
    score = profit + revenue + accel if profit > 0 else 0.0
    return round(score, 2), {"profit": round(profit, 2), "revenue": round(revenue, 2), "accel": accel,
                             "np_yoy": g, "rev_yoy": rev, "prev_np_yoy": prev,
                             "period": c.get("period"), "turnaround": c.get("turnaround", False)}


def score_a(a: dict, cfg: dict) -> tuple[float, dict]:
    w = cfg["weight"]
    if not a.get("available"):
        return 0.0, {"note": "无年报"}
    growth_w, roe_w = w * 0.6, w * 0.4
    g = [x["yoy"] for x in a["growth"]]
    n_ok = sum(1 for x in g if x is not None and x >= cfg["growth_min"])
    growth = growth_w * n_ok / cfg["years"]
    roe = _num(a.get("roe"))
    roe_s = _lin(roe, cfg["roe_start"], cfg["roe_full"], roe_w / 2, roe_w) \
        if roe is not None and roe >= cfg["roe_start"] else 0.0
    return round(growth + roe_s, 2), {"growth": round(growth, 2), "roe_score": round(roe_s, 2),
                                      "years_ok": n_ok, "yoy": g, "roe": roe}


def score_n(dist: float, cfg: dict) -> tuple[float, dict]:
    w = cfg["weight"]
    s = w if dist <= cfg["full_dist"] else (w / 2 if dist <= cfg["half_dist"] else 0.0)
    return s, {"dist_high": dist}


def score_s(vol_ratio, mcap_yi, cfg: dict) -> tuple[float, dict]:
    w = cfg["weight"]
    vol_w, cap_w = w * 0.8, w * 0.2
    v = _lin(_num(vol_ratio), 1.0, cfg["vol_ratio"], 0, vol_w)
    if mcap_yi is None:
        cap = cap_w / 2
    else:
        cap = cap_w if mcap_yi <= cfg["small_cap_yi"] else (cap_w / 2 if mcap_yi <= cfg["small_cap_yi"] * 3 else 0)
    return round(v + cap, 2), {"up_vol_ratio": _num(vol_ratio), "float_mcap_yi": mcap_yi}


def score_l(rs, sector_pct_rank, cfg: dict) -> tuple[float, dict]:
    w = cfg["weight"]
    rs_w, sec_w = w * 0.8, w * 0.2
    rs = _num(rs)
    rs_s = _lin(rs, cfg["rs_start"], cfg["rs_full"], rs_w / 2, rs_w) if rs is not None and rs >= cfg["rs_start"] else 0.0
    sec = sec_w if (rs_s > 0 and sector_pct_rank is not None and sector_pct_rank <= cfg["sector_top_pct"]) else 0.0
    return round(rs_s + sec, 2), {"rs": rs, "sector_rank_pct": sector_pct_rank}


def score_i(org_net, turnover_pct, cfg: dict, supported: bool) -> tuple[float, dict]:
    w = cfg["weight"]
    lhb_w, liq_w = w * 0.6, w * 0.4
    lhb = lhb_w if (supported and org_net is not None and org_net > 0) else 0.0
    liq = liq_w * (_num(turnover_pct) or 0) / 100
    return round(lhb + liq, 2), {"org_net_60d": org_net, "turnover_pct": _num(turnover_pct),
                                 "proxy": True, "org_supported": supported}


def score_stock(row: dict, fin_c: dict, fin_a: dict, scfg: dict, org_supported: bool) -> dict:
    parts, details = {}, {}
    parts["C"], details["C"] = score_c(fin_c, scfg["C"])
    parts["A"], details["A"] = score_a(fin_a, scfg["A"])
    parts["N"], details["N"] = score_n(row["dist_high"], scfg["N"])
    parts["S"], details["S"] = score_s(row.get("up_vol_ratio"), row.get("float_mcap_yi"), scfg["S"])
    parts["L"], details["L"] = score_l(row.get("rs"), row.get("sector_rank_pct"), scfg["L"])
    parts["I"], details["I"] = score_i(row.get("org_net_60d"), row.get("turnover_pct"), scfg["I"], org_supported)
    total = round(sum(parts.values()), 2)
    zero = [k for k in scfg["required_nonzero"] if parts[k] <= 0]
    passed = total >= scfg["pass_score"] and not zero
    return {"total": total, "factors": parts, "details": details, "zero_factors": zero, "passed": passed}
