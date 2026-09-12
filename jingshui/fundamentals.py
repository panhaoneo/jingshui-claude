"""财务口径的三个 A 股特有处理（docs/03 L2）：

1. 单季化：定期报告是年初至今累计口径，单季值 = 本期累计 − 同年上期累计（Q1 除外）；
2. 同比基准：与去年同一季度比，不与本年上一季度比；
3. 时点纪律：只使用 report_date ≤ asof 的报告（披露日而非报告期末），避免未来函数。

null 不是 0：缺失一律保持 None，不补零。基数 ≤0 时同比无意义（扭亏/亏损扩大），返回 None。
"""
from __future__ import annotations

import pandas as pd


def _known(records: list[dict], asof: pd.Timestamp) -> list[dict]:
    out = []
    for r in records or []:
        rd = r.get("report_date")
        if rd is None or pd.Timestamp(rd) <= asof:
            out.append(r)
    return out


def yoy(cur, base) -> float | None:
    if cur is None or base is None or pd.isna(cur) or pd.isna(base) or base <= 0:
        return None
    return cur / base - 1


def single_quarters(quarterly: list[dict], asof: pd.Timestamp) -> pd.DataFrame:
    """累计口径 → 单季。返回按 (fiscal_year, quarter) 升序的 [fy, q, period_end, report_date, revenue, np]。"""
    recs = [r for r in _known(quarterly, asof) if r.get("quarter") and r.get("fiscal_year")]
    if not recs:
        return pd.DataFrame(columns=["fy", "q", "period_end", "report_date", "revenue", "np"])
    cum = {(r["fiscal_year"], r["quarter"]): r for r in recs}
    rows = []
    for (fy, q), r in sorted(cum.items()):
        if q == 1:
            rev, np_ = r.get("revenue"), r.get("parent_np")
        else:
            prev = cum.get((fy, q - 1))
            if prev is None:
                rev = np_ = None
            else:
                rev = None if r.get("revenue") is None or prev.get("revenue") is None \
                    else r["revenue"] - prev["revenue"]
                np_ = None if r.get("parent_np") is None or prev.get("parent_np") is None \
                    else r["parent_np"] - prev["parent_np"]
        rows.append({"fy": fy, "q": q, "period_end": r["period_end"], "report_date": r.get("report_date"),
                     "revenue": rev, "np": np_, "cum_np": r.get("parent_np")})
    return pd.DataFrame(rows)


def quarter_growth(sq: pd.DataFrame) -> pd.DataFrame:
    """在单季表上加同比：np_yoy / rev_yoy（与去年同季比）。"""
    if sq.empty:
        return sq.assign(np_yoy=[], rev_yoy=[])
    idx = {(r.fy, r.q): r for r in sq.itertuples()}
    np_yoy, rev_yoy = [], []
    for r in sq.itertuples():
        base = idx.get((r.fy - 1, r.q))
        np_yoy.append(yoy(r.np, base.np) if base is not None else None)
        rev_yoy.append(yoy(r.revenue, base.revenue) if base is not None else None)
    return sq.assign(np_yoy=np_yoy, rev_yoy=rev_yoy)


def c_metrics(fin: dict, asof: pd.Timestamp) -> dict:
    g = quarter_growth(single_quarters(fin.get("quarterly", []), asof))
    if g.empty:
        return {"available": False}
    last = g.iloc[-1]
    prev = g.iloc[-2] if len(g) > 1 else None
    hist = [{"period": f"{int(r.fy)}Q{int(r.q)}", "np": r.np, "revenue": r.revenue,
             "np_yoy": r.np_yoy, "rev_yoy": r.rev_yoy} for r in g.tail(8).itertuples()]
    return {
        "available": True,
        "period": f"{int(last.fy)}Q{int(last.q)}",
        "report_date": last.report_date,
        "np": last.np,
        "np_yoy": last.np_yoy,
        "rev_yoy": last.rev_yoy,
        "prev_np_yoy": None if prev is None else prev.np_yoy,
        "cum_np": last.cum_np,
        "turnaround": bool(last.np is not None and last.np > 0 and last.np_yoy is None),
        "history": hist,
    }


def a_metrics(fin: dict, asof: pd.Timestamp, years: int = 3) -> dict:
    annual = sorted(_known(fin.get("annual", []), asof), key=lambda r: r["fiscal_year"])
    annual = [r for r in annual if r.get("quarter") in (4, None)]
    by_year = {r["fiscal_year"]: r.get("parent_np") for r in annual}
    ys = sorted(by_year)
    growth = []
    for y in ys[-years:]:
        growth.append({"year": y, "np": by_year[y], "yoy": yoy(by_year[y], by_year.get(y - 1))})
    bal = {r["fiscal_year"]: r.get("equity") for r in _known(fin.get("balance", []), asof)}
    roe = None
    if ys:
        y = ys[-1]
        eq_end, eq_beg = bal.get(y), bal.get(y - 1)
        np_ = by_year[y]
        if np_ is not None and eq_end:
            avg = (eq_end + eq_beg) / 2 if eq_beg else eq_end
            roe = np_ / avg if avg and avg > 0 else None
    return {"available": bool(ys), "growth": growth, "roe": roe, "roe_year": ys[-1] if ys else None}


def growth_decelerating(c: dict, drop_ratio: float) -> dict:
    """卖出触发：单季净利或营收增速连续两个季度大幅回落（每季降幅 ≥ drop_ratio）。"""
    hist = c.get("history") or []
    out = {}
    for key in ("np_yoy", "rev_yoy"):
        vals = [h[key] for h in hist[-3:]]
        if len(vals) == 3 and all(v is not None for v in vals) and vals[0] > 0:
            g0, g1, g2 = vals
            out[key] = bool(g1 <= g0 * (1 - drop_ratio) and g2 <= g1 * (1 - drop_ratio) if g1 > 0
                            else g2 < g1 <= g0 * (1 - drop_ratio))
        else:
            out[key] = False
    return out
