"""全市场技术指标（向量化，基于前复权宽表）。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .models import PricePanel


def returns_over(close_ff: pd.DataFrame, days: int) -> pd.Series:
    if len(close_ff) <= days:
        return pd.Series(np.nan, index=close_ff.columns)
    return close_ff.iloc[-1] / close_ff.iloc[-1 - days] - 1


def rs_score(close_ff: pd.DataFrame, bars: pd.Series, days: int, method: str) -> pd.Series:
    """RS 相对强度百分位（1~99 口径的 0~100 实现），样本总体 = 全市场有足够历史的股票。"""
    if method == "ibd":  # IBD 口径：近一季权重加倍
        r = (0.4 * returns_over(close_ff, 63) + 0.2 * returns_over(close_ff, 126)
             + 0.2 * returns_over(close_ff, 189) + 0.2 * returns_over(close_ff, 252))
    else:
        r = returns_over(close_ff, days)
    r = r.where(bars >= days)
    return r.rank(pct=True) * 100


def rs_history(close_ff: pd.DataFrame, days: int, last_n: int = 130) -> pd.DataFrame:
    """每日 RS 百分位（用于持仓体检的 RS 从 ≥80 跌至 <60 判定）。"""
    ret = close_ff / close_ff.shift(days) - 1
    return ret.tail(last_n).rank(axis=1, pct=True) * 100


def market_metrics(panel: PricePanel, scfg: dict) -> pd.DataFrame:
    close = panel.close
    close_ff = close.ffill()
    bars = close.notna().sum()
    last = close_ff.iloc[-1]
    traded_today = close.iloc[-1].notna()
    high250 = panel.high.tail(250).max()
    vol = panel.volume
    vol_ma20_prev = vol.shift(1).rolling(20, min_periods=10).mean()
    vol_ratio = vol / vol_ma20_prev
    up = close_ff > close_ff.shift(1)
    lb = scfg["S"]["lookback"]
    up_vol_ratio = vol_ratio.where(up).tail(lb).max()
    ma50 = close_ff.tail(50).mean()
    ma200 = close_ff.tail(200).mean().where(bars >= 200)

    df = pd.DataFrame({
        "bars": bars,
        "close": last,
        "raw_close": panel.raw_close.ffill().iloc[-1],
        "chg_pct": (close_ff.iloc[-1] / close_ff.iloc[-2] - 1) * 100,
        "traded_today": traded_today,
        "high250": high250,
        "dist_high": 1 - last / high250,
        "ret250": returns_over(close_ff, scfg["rs_days"]),
        "ret60": returns_over(close_ff, 60),
        "ret20": returns_over(close_ff, 20),
        "rs": rs_score(close_ff, bars, scfg["rs_days"], scfg.get("rs_method", "simple")),
        "avg_turnover20": panel.turnover.tail(20).mean(),
        "vol_ratio": vol_ratio.iloc[-1],
        "up_vol_ratio": up_vol_ratio,
        "ma50": ma50,
        "ma200": ma200,
    })
    df.index.name = "thscode"
    return df
