"""由复权事件（现金分红/送股/配股）推算前复权因子。

与同花顺 marketdb 的口径一致：
    ratio = ((1 + s + r) * close_pre) / (close_pre - d + r * p)
在事件生效日（除权日当天或之后的第一个交易日）生效；
backward[t] = 截至 t 的 ratio 连乘；forward[t] = backward[t] / backward[最后一日]。
窗口之前的事件对所有日期同比例缩放，前复权归一化后自然抵消，因此只需窗口内事件。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def forward_factors(kline: pd.DataFrame, events: pd.DataFrame) -> pd.Series:
    """kline: 长表 [thscode, date, close]（未复权）；events: [thscode, ex_date, d, s, r, p]。
    返回与 kline 行对齐的前复权因子（最新一日 = 1）。"""
    k = kline[["thscode", "date", "close"]].copy()
    k["_row"] = np.arange(len(k))
    k = k.sort_values(["thscode", "date"])
    k["prev_close"] = k.groupby("thscode", sort=False)["close"].shift(1)

    ratio = pd.Series(1.0, index=k.index)
    if events is not None and len(events):
        ev = events.dropna(subset=["ex_date"]).copy()
        for col in ("d", "s", "r", "p"):
            ev[col] = pd.to_numeric(ev[col], errors="coerce").fillna(0.0)
        ev = ev[ev["thscode"].isin(k["thscode"].unique())]
        if len(ev):
            ev = ev.sort_values("ex_date")
            dates = k[["thscode", "date"]].sort_values("date")
            eff = pd.merge_asof(ev, dates.rename(columns={"date": "eff_date"}),
                                left_on="ex_date", right_on="eff_date", by="thscode",
                                direction="forward")
            eff = eff.dropna(subset=["eff_date"])
            eff = eff.merge(k[["thscode", "date", "prev_close"]],
                            left_on=["thscode", "eff_date"], right_on=["thscode", "date"], how="inner")
            denom = eff["prev_close"] - eff["d"] + eff["r"] * eff["p"]
            eff["ratio"] = (eff["prev_close"] * (1.0 + eff["s"] + eff["r"])) / denom.where(denom > 0)
            eff = eff[(eff["ratio"] > 0) & np.isfinite(eff["ratio"])]
            day = eff.groupby(["thscode", "eff_date"])["ratio"].prod()
            idx = pd.MultiIndex.from_frame(k[["thscode", "date"]])
            ratio = pd.Series(day.reindex(idx).fillna(1.0).to_numpy(), index=k.index)

    backward = ratio.groupby(k["thscode"], sort=False).cumprod()
    last = backward.groupby(k["thscode"], sort=False).transform("last")
    fwd = backward / last
    out = pd.Series(fwd.to_numpy(), index=k["_row"].to_numpy()).sort_index()
    return pd.Series(out.to_numpy(), index=kline.index)
