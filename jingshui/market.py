"""L0 大盘闸门 M（欧奈尔派发日计数的 A 股实现）。

派发日：指数收跌 ≥0.2% 且成交量 > 前一日。
OFFENSE：收盘 > MA50 > MA200，且近 25 日派发日 ≤ 2；
DEFENSE：跌破 MA50 且近 25 日派发日 ≥ 5，或 MA50 < MA200；
CAUTION：其余。三个指数分别判定，取最保守者为全局状态。
"""
from __future__ import annotations

import pandas as pd

SEVERITY = {"OFFENSE": 0, "CAUTION": 1, "DEFENSE": 2}
LABEL = {"OFFENSE": "进攻", "CAUTION": "观望", "DEFENSE": "防守"}


def distribution_flags(df: pd.DataFrame, drop_pct: float) -> pd.Series:
    chg = df["close"].pct_change() * 100
    return (chg <= drop_pct) & (df["volume"] > df["volume"].shift(1))


def evaluate_index(df: pd.DataFrame, mcfg: dict) -> dict:
    df = df.sort_values("date").reset_index(drop=True)
    close = df["close"]
    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(200).mean()
    dd = distribution_flags(df, mcfg["dd_drop_pct"])
    flags = dd.tail(mcfg["dd_window"])
    dd_dates = [d.strftime("%Y-%m-%d") for d in df.loc[flags[flags].index, "date"]]
    n_dd = len(dd_dates)

    c, m50, m200 = close.iloc[-1], ma50.iloc[-1], ma200.iloc[-1]
    bull = bool(c > m50 > m200)
    if pd.isna(m200):
        state, reason = "CAUTION", "历史不足 200 日，无法判定多头排列"
    elif (c < m50 and n_dd >= mcfg["defense_min_dd"]) or m50 < m200:
        state = "DEFENSE"
        reason = "MA50 下穿 MA200" if m50 < m200 else f"跌破 MA50 且派发日 {n_dd} 个"
    elif bull and n_dd <= mcfg["offense_max_dd"]:
        state, reason = "OFFENSE", f"多头排列，派发日 {n_dd} 个"
    else:
        parts = []
        if not bull:
            parts.append("未形成 收盘>MA50>MA200")
        if n_dd > mcfg["offense_max_dd"]:
            parts.append(f"派发日 {n_dd} 个（>{mcfg['offense_max_dd']}）")
        state, reason = "CAUTION", "；".join(parts)

    tail = df.tail(250)
    chart = {
        "d": [x.strftime("%Y%m%d") for x in tail["date"]],
        "c": [round(float(x), 2) for x in tail["close"]],
        "ma50": [None if pd.isna(x) else round(float(x), 2) for x in ma50.tail(250)],
        "ma200": [None if pd.isna(x) else round(float(x), 2) for x in ma200.tail(250)],
        "dd": [bool(x) for x in dd.tail(250)],
    }
    prev = close.iloc[-2] if len(close) > 1 else c
    return {
        "date": df["date"].iloc[-1].strftime("%Y-%m-%d"),
        "close": round(float(c), 2),
        "chg_pct": round(float((c / prev - 1) * 100), 2),
        "ma50": None if pd.isna(m50) else round(float(m50), 2),
        "ma200": None if pd.isna(m200) else round(float(m200), 2),
        "dd_count": n_dd,
        "dd_dates": dd_dates,
        "state": state,
        "reason": reason,
        "chart": chart,
    }


def market_gate(index_frames: dict[str, pd.DataFrame], names: dict[str, str], mcfg: dict) -> dict:
    results = []
    for code, df in index_frames.items():
        if df is None or len(df) < 60:
            continue
        r = evaluate_index(df, mcfg)
        r.update(code=code, name=names.get(code, code))
        results.append(r)
    if not results:
        state = "DEFENSE"
        reason = "指数数据缺失，按最保守处理"
    else:
        worst = max(results, key=lambda r: SEVERITY[r["state"]])
        state, reason = worst["state"], f"{worst['name']}：{worst['reason']}"
    return {
        "state": state,
        "label": LABEL[state],
        "position_cap": mcfg["position_cap"][state],
        "reason": reason,
        "allow_new": state == "OFFENSE",
        "indices": results,
    }
