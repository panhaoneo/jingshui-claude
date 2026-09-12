"""L1 行业景气度："当下哪个行业最景气"的量化。

| 指标     | 计算                                   | 权重 |
|----------|----------------------------------------|------|
| 板块 RS  | 近 120 日涨幅在全部板块中的百分位      | 40   |
| 创新高   | 距 250 日最高价（≤2% 满分）            | 30   |
| 中期动能 | 近 60 日涨幅百分位                     | 20   |
| 趋势健康 | 指数在 MA50 上方且 MA50 > MA200        | 10   |

必要条件：板块指数接近新高（静水原文的判据就是"行业指数已创新高"）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sector_metrics(df: pd.DataFrame, scfg: dict) -> dict | None:
    df = df.sort_values("date")
    close, high = df["close"].reset_index(drop=True), df["high"].reset_index(drop=True)
    n = len(close)
    if n < max(scfg["rs_days"], scfg["mom_days"]) + 1:
        return None
    c = float(close.iloc[-1])
    hi = float(high.tail(scfg["high_days"]).max())
    ma50 = close.rolling(50).mean().iloc[-1]
    ma200 = close.rolling(200).mean().iloc[-1] if n >= 200 else np.nan
    return {
        "close": c,
        "chg_pct": float(c / close.iloc[-2] - 1) * 100,
        "ret_rs": float(c / close.iloc[-1 - scfg["rs_days"]] - 1),
        "ret_mom": float(c / close.iloc[-1 - scfg["mom_days"]] - 1),
        "ret_20": float(c / close.iloc[-21] - 1) if n > 21 else np.nan,
        "dist_high": float(1 - c / hi) if hi > 0 else np.nan,
        "trend_ok": bool(c > ma50 and not np.isnan(ma200) and ma50 > ma200),
        "date": df["date"].iloc[-1],
    }


def score_sectors(frames: dict[str, pd.DataFrame], names: dict[str, str], scfg: dict) -> pd.DataFrame:
    rows = []
    for code, df in frames.items():
        m = sector_metrics(df, scfg) if df is not None and len(df) else None
        if m:
            rows.append({"code": code, "name": names.get(code, code), **m})
    if not rows:
        return pd.DataFrame()
    t = pd.DataFrame(rows)
    w = scfg["weights"]
    t["rs_pct"] = t["ret_rs"].rank(pct=True) * 100
    t["mom_pct"] = t["ret_mom"].rank(pct=True) * 100
    full, zero = scfg["high_full_dist"], scfg["high_zero_dist"]
    t["high_score"] = w["high"] * ((zero - t["dist_high"]) / (zero - full)).clip(0, 1)
    t["score"] = (w["rs"] * t["rs_pct"] / 100 + t["high_score"] + w["mom"] * t["mom_pct"] / 100
                  + w["trend"] * t["trend_ok"].astype(float))
    need = scfg.get("require_near_high")
    t["eligible"] = True if need is None else t["dist_high"] <= need
    t = t.sort_values("score", ascending=False).reset_index(drop=True)
    t["rank"] = np.arange(1, len(t) + 1)
    mainline_codes = t[t["eligible"]].head(scfg["top_n"])["code"]
    t["mainline"] = t["code"].isin(mainline_codes)
    return t
