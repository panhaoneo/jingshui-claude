from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

TZ = "Asia/Shanghai"


def ms_to_date(ms) -> pd.Series | pd.Timestamp:
    """毫秒时间戳（Asia/Shanghai 零点）→ 无时区日期。"""
    ts = pd.to_datetime(ms, unit="ms", utc=True)
    if isinstance(ts, pd.Timestamp):
        return ts.tz_convert(TZ).tz_localize(None).normalize()
    return ts.dt.tz_convert(TZ).dt.tz_localize(None).dt.normalize()


def date_to_ms(d) -> int:
    ts = pd.Timestamp(d)
    if ts.tzinfo is None:
        ts = ts.tz_localize(TZ)
    return int(ts.timestamp() * 1000)


@dataclass
class PricePanel:
    """全市场前复权日线宽表：index=交易日，columns=thscode。
    价格为前复权；volume 同步做了复权（送转后成交量可比）；turnover 为原始成交额（元）。"""
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    turnover: pd.DataFrame
    raw_close: pd.DataFrame
    source: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.close.index

    @property
    def asof(self) -> pd.Timestamp:
        return self.close.index[-1]

    def series(self, code: str) -> pd.DataFrame:
        df = pd.DataFrame({
            "open": self.open[code], "high": self.high[code], "low": self.low[code],
            "close": self.close[code], "volume": self.volume[code], "turnover": self.turnover[code],
        })
        return df.dropna(subset=["close"])


def long_to_panel(df: pd.DataFrame, source: str) -> PricePanel:
    """长表 [thscode, date, open, high, low, close, volume, turnover, factor] → PricePanel。"""
    f = df["factor"] if "factor" in df else 1.0
    adj = df.assign(open=df["open"] * f, high=df["high"] * f, low=df["low"] * f,
                    close_adj=df["close"] * f, volume=df["volume"] / f)
    piv = lambda col: adj.pivot(index="date", columns="thscode", values=col).sort_index()
    return PricePanel(open=piv("open"), high=piv("high"), low=piv("low"), close=piv("close_adj"),
                      volume=piv("volume"), turnover=piv("turnover"), raw_close=piv("close"),
                      source=source)
