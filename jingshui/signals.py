"""L3 买点与卖出触发。

买点（融合冲突 A：回调本身不是买入信号，回调之后的重新起涨才是）三条同时成立：
1. 趋势未破：未有效跌破 MA50，且 MA50 > MA200；
2. 回调幅度正常：自阶段高点的回撤深度在 8%~25%（>30% 是弱者，直接出局）；
3. 起涨确认：放量上攻（量 ≥1.4×MA20 量且收涨）或突破回调平台高点。
第 3 条不成立只入观察池，不下单。已超出买点 5% 不追（欧奈尔）。

卖出（融合冲突 B：不设固定止盈，只看卖出触发条件）。
"""
from __future__ import annotations

import pandas as pd


def _eval_at(df: pd.DataFrame, i: int, b: dict) -> dict:
    """以第 i 根 K 线为"今天"评估买点。df 为单只股票前复权日线（按日期升序，已去 NaN）。"""
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    c = float(close.iloc[i])
    ma50 = float(close.iloc[max(0, i - 49): i + 1].mean())
    ma200 = float(close.iloc[max(0, i - 199): i + 1].mean()) if i >= 199 else float("nan")
    lo = max(0, i - b["stage_high_days"])
    win = high.iloc[lo:i]  # 阶段高点：不含"今天"
    if win.empty:
        return {"ok": False, "status": "数据不足"}
    ih = int(win.values.argmax()) + lo
    stage_high = float(high.iloc[ih])
    pull_low = float(low.iloc[ih:i].min()) if ih < i else float(low.iloc[i])
    depth = 1 - pull_low / stage_high
    cur_dd = 1 - c / stage_high
    vol_ma20 = float(vol.iloc[max(0, i - 20): i].mean())
    vr = float(vol.iloc[i] / vol_ma20) if vol_ma20 > 0 else float("nan")
    up = c > float(close.iloc[i - 1])
    plat_lo = max(ih, i - b["platform_days"])
    platform_high = float(high.iloc[plat_lo:i].max()) if plat_lo < i else stage_high
    vol_attack = bool(up and vr >= b["restart_vol_ratio"])
    platform_break = bool(c > platform_high and ih < i - 1)
    new_high_break = bool(c > stage_high)
    pivot = stage_high if new_high_break else platform_high

    info = {
        "close": round(c, 3), "ma50": round(ma50, 3), "ma200": None if pd.isna(ma200) else round(ma200, 3),
        "stage_high": round(stage_high, 3), "stage_high_date": df.index[ih].strftime("%Y-%m-%d"),
        "pullback_low": round(pull_low, 3), "depth": round(depth, 4), "cur_dd": round(cur_dd, 4),
        "vol_ratio": None if pd.isna(vr) else round(vr, 2), "platform_high": round(platform_high, 3),
        "pivot": round(pivot, 3), "vol_attack": vol_attack, "platform_break": platform_break,
    }
    trend_ok = not pd.isna(ma200) and ma50 > ma200
    broken = c < ma50 * (1 - b["ma50_break_tol"])
    if not trend_ok:
        return {**info, "ok": False, "status": "趋势未确立（MA50 ≤ MA200）"}
    if broken:
        return {**info, "ok": False, "status": "已有效跌破 MA50"}
    if depth > b["pullback_out"]:
        return {**info, "ok": False, "status": f"阶段回撤 {depth:.0%}（>30%），形态过深，弱者出局"}
    if depth > b["pullback_max"]:
        return {**info, "ok": False, "status": f"回撤 {depth:.0%} 超出 25%，不做"}
    if depth < b["pullback_min"]:
        return {**info, "ok": False, "status": f"未充分回调（回撤 {depth:.0%} < 8%），等待回调"}
    if not (vol_attack or platform_break):
        return {**info, "ok": False, "status": "回调到位，等待起涨确认"}
    kind = "放量上攻" if vol_attack else "突破回调平台"
    if vol_attack and platform_break:
        kind = "放量突破平台"
    return {**info, "ok": True, "status": f"起涨确认：{kind}", "kind": kind}


def buy_point(df: pd.DataFrame, b: dict, confirm_days: int = 3) -> dict:
    """今天是否满足买点；若近 confirm_days 日内已确认且现价未超出买点 5%，仍视为有效。"""
    n = len(df)
    if n < 60:
        return {"ok": False, "status": "数据不足"}
    today = _eval_at(df, n - 1, b)
    if today.get("ok"):
        today["signal_date"] = df.index[-1].strftime("%Y-%m-%d")
        return _finalize(today, float(df["close"].iloc[-1]), b)
    for k in range(2, confirm_days + 1):
        if n - k < 60:
            break
        past = _eval_at(df, n - k, b)
        if past.get("ok"):
            c = float(df["close"].iloc[-1])
            if c < past["ma50"] * (1 - b["ma50_break_tol"]):
                break
            past["signal_date"] = df.index[n - k].strftime("%Y-%m-%d")
            past["status"] += f"（T-{k - 1}）"
            return _finalize(past, c, b)
    return today


def _finalize(sig: dict, close: float, b: dict) -> dict:
    ext = close / sig["pivot"] - 1 if sig["pivot"] else 0
    sig["extension"] = round(ext, 4)
    if ext > b["chase_limit"]:
        sig["ok"] = False
        sig["status"] = f"已超出买点 {ext:.1%}（>5%），不追"
        return sig
    sig["entry"] = round(close, 3)
    sig["stops"] = [round(close * (1 - s), 3) for s in b["stop_loss"]]
    return sig


# ---------------- 持仓体检 ----------------
def check_holding(code: str, h: dict, df: pd.DataFrame, rs_hist: pd.Series | None, c_metrics: dict | None,
                  m_state: str, cfg: dict) -> dict:
    from .fundamentals import growth_decelerating

    s, b = cfg["sell"], cfg["buy"]
    close = df["close"]
    c = float(close.iloc[-1])
    ma50 = close.rolling(50).mean()
    vol_ratio = df["volume"] / df["volume"].shift(1).rolling(20).mean()
    batches = h.get("batches") or []
    stop = b["stop_loss_defense"] if m_state == "DEFENSE" else b["stop_loss"][0]
    actions, notes = [], []

    for bt in batches:
        stop_px = bt["price"] * (1 - stop)
        if c <= stop_px:
            actions.append(f"止损：{bt['date']} 批次（成本 {bt['price']}）跌破 {stop:.0%} 止损线 {stop_px:.2f}")

    if c_metrics and c_metrics.get("available"):
        dec = growth_decelerating(c_metrics, s["growth_drop_ratio"])
        if dec.get("np_yoy") or dec.get("rev_yoy"):
            actions.append("清仓：单季净利/营收增速连续两季回落 ≥1/3")

    below = close < ma50
    recent = df.tail(12)
    for j, (d, row) in enumerate(recent.iterrows()):
        k = df.index.get_loc(d)
        if k == 0 or pd.isna(ma50.iloc[k]):
            continue
        crossed = below.iloc[k] and not below.iloc[k - 1] and vol_ratio.iloc[k] >= b["restart_vol_ratio"]
        if crossed:
            after = below.iloc[k:]
            days = len(after) - 1
            if after.all() and days >= s["ma50_break_days"]:
                actions.append(f"清仓：{d:%m-%d} 放量跌破 50 日线，{days} 日未收回")
            elif after.all():
                notes.append(f"{d:%m-%d} 放量跌破 50 日线，观察 {s['ma50_break_days']} 日内能否收回")

    if rs_hist is not None and batches:
        first = pd.Timestamp(min(bt["date"] for bt in batches))
        since = rs_hist[rs_hist.index >= first].dropna()
        if len(since) and since.max() >= s["rs_from"] and since.iloc[-1] < s["rs_to"]:
            actions.append(f"清仓：RS 从 {since.max():.0f} 跌至 {since.iloc[-1]:.0f}（<{s['rs_to']}）")

    if m_state == "DEFENSE":
        actions.append("减仓：大盘转入 DEFENSE，至少减半")

    if batches:
        first = pd.Timestamp(min(bt["date"] for bt in batches))
        p0 = [bt["price"] for bt in batches if pd.Timestamp(bt["date"]) == first][0]
        after = close[close.index >= first]
        if len(after):
            wk3 = after.head(15)
            if wk3.max() >= p0 * (1 + s["hold_8w_gain"]):
                until = first + pd.Timedelta(weeks=8)
                if df.index[-1] < until:
                    notes.append(f"3 周内涨幅 ≥20%：锁定持有至 {until:%Y-%m-%d}（8 周）再评估")
        if not pd.isna(ma50.iloc[-1]) and abs(c / ma50.iloc[-1] - 1) < 0.01 and not below.iloc[-1]:
            notes.append("触及 50 日线未跌破：不动（欧奈尔规则）")

    cost = sum(bt["price"] * bt.get("weight", 0) for bt in batches)
    wsum = sum(bt.get("weight", 0) for bt in batches)
    return {
        "code": code, "close": round(c, 3),
        "pnl": round(c / (cost / wsum) - 1, 4) if wsum else None,
        "actions": actions, "notes": notes,
        "verdict": "卖出/减仓" if actions else "继续持有",
    }
