"""测试用的合成数据构造器。全部离线, 不需要 API Key。"""

from __future__ import annotations

import datetime as _dt

from jingshui.indicators import Bar

DAY_MS = 86_400_000
BASE_MS = 1_700_000_000_000  # 2023-11-14 左右


def make_bars(
    closes: list[float],
    volumes: list[float] | None = None,
    start_ms: int = BASE_MS,
) -> list[Bar]:
    """按收盘价序列造日 K。high/low 围绕 close 做 ±1% 包络。"""
    vols = volumes or [1_000_000.0] * len(closes)
    assert len(vols) == len(closes)
    bars = []
    for i, (c, v) in enumerate(zip(closes, vols)):
        bars.append(
            Bar(
                date_ms=start_ms + i * DAY_MS,
                open=c,
                high=c * 1.01,
                low=c * 0.99,
                close=c,
                volume=v,
                turnover=c * v,
            )
        )
    return bars


def trend_bars(n: int, start: float = 10.0, daily: float = 0.004, volume: float = 5e6):
    """一条平滑上行的价格序列。"""
    closes = [start * ((1 + daily) ** i) for i in range(n)]
    return make_bars(closes, [volume] * n)


def period_end_ms(year: int, quarter: int) -> int:
    month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[quarter]
    dt = _dt.datetime(year, month_day[0], month_day[1], tzinfo=_dt.timezone.utc)
    return int(dt.timestamp() * 1000)


def report_date_ms(year: int, quarter: int) -> int:
    """A 股法定披露截止日: Q1 4/30, H1 8/31, Q3 10/31, FY 次年 4/30。"""
    mapping = {1: (year, 4, 30), 2: (year, 8, 31), 3: (year, 10, 31), 4: (year + 1, 4, 30)}
    y, m, d = mapping[quarter]
    return int(_dt.datetime(y, m, d, tzinfo=_dt.timezone.utc).timestamp() * 1000)


def income_row(
    year: int,
    quarter: int,
    cumulative_profit: float,
    cumulative_revenue: float,
    cumulative_eps: float = 0.0,
    period: str = "quarterly",
) -> dict:
    """一行累计口径的利润表。"""
    return {
        "thscode": "000001.SZ",
        "ticker": "000001",
        "period": period,
        "period_end_ms": period_end_ms(year, quarter),
        "report_date_ms": report_date_ms(year, quarter),
        "fiscal_year": year,
        "fiscal_period": {1: "Q1", 2: "H1", 3: "Q3", 4: "FY"}[quarter],
        "currency": "CNY",
        "basic_eps": cumulative_eps,
        "operating_income": cumulative_revenue,
        "parent_holder_net_profit": cumulative_profit,
        "net_profit": cumulative_profit,
    }


def annual_income(year: int, profit: float, revenue: float = 0.0) -> dict:
    return income_row(year, 4, profit, revenue or profit * 5, period="annual")


def balance_row(year: int, equity: float) -> dict:
    return {
        "thscode": "000001.SZ",
        "period": "annual",
        "period_end_ms": period_end_ms(year, 4),
        "report_date_ms": report_date_ms(year, 4),
        "fiscal_year": year,
        "fiscal_period": "FY",
        "holder_equity_total": equity,
        "assets_total": equity * 2,
    }
