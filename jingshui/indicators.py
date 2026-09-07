"""纯计算层: 只接受价量序列, 不发网络请求。

所有函数都对"数据不足"返回 None 而不是抛异常或补零 —— 契约明确要求
`null` 表示未披露, 调用方不得自动补零。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Bar:
    """一根日 K。"""

    date_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float = 0.0

    @classmethod
    def from_api(cls, row: dict) -> "Bar":
        return cls(
            date_ms=int(row["date_ms"]),
            open=float(row["open_price"]),
            high=float(row["high_price"]),
            low=float(row["low_price"]),
            close=float(row["close_price"]),
            volume=float(row["volume"]),
            turnover=float(row.get("turnover") or 0.0),
        )


def to_bars(rows: Sequence[dict]) -> list[Bar]:
    """把 API 返回的 K 线行转成按时间升序的 Bar 列表。"""
    bars = [Bar.from_api(r) for r in rows]
    bars.sort(key=lambda b: b.date_ms)
    return bars


def sma(values: Sequence[float], window: int) -> float | None:
    """最后 window 个值的简单均值。数据不足返回 None。"""
    if window <= 0 or len(values) < window:
        return None
    return sum(values[-window:]) / window


def closes(bars: Sequence[Bar]) -> list[float]:
    return [b.close for b in bars]


def volumes(bars: Sequence[Bar]) -> list[float]:
    return [b.volume for b in bars]


def pct_change(new: float, old: float) -> float | None:
    """相对变化。基数为 0 或负数时返回 None —— 负基数的增长率没有意义。"""
    if old is None or new is None or old <= 0:
        return None
    return (new - old) / old


def trailing_return(bars: Sequence[Bar], window: int) -> float | None:
    """近 window 个交易日的收益率。"""
    if len(bars) < window + 1:
        return None
    return pct_change(bars[-1].close, bars[-1 - window].close)


def distance_from_high(bars: Sequence[Bar], window: int = 250) -> float | None:
    """当前收盘价距 window 日内最高价的回撤比例 (0 表示正在创新高)。

    对应 N 因子: 距新高越近越好。
    """
    if not bars:
        return None
    span = bars[-window:] if len(bars) >= window else bars
    peak = max(b.high for b in span)
    if peak <= 0:
        return None
    return (peak - bars[-1].close) / peak


def volume_ratio(bars: Sequence[Bar], window: int = 20) -> float | None:
    """当日成交量 / 前 window 日均量。

    对应 S 因子: 欧奈尔要求突破日放量 >= 1.4~1.5 倍。
    注意分母排除当日, 否则放量会被自己稀释。
    """
    if len(bars) < window + 1:
        return None
    base = sma(volumes(bars[:-1]), window)
    if not base:
        return None
    return bars[-1].volume / base


def avg_turnover(bars: Sequence[Bar], window: int = 20) -> float | None:
    """近 window 日的日均成交额, 用于流动性门槛。"""
    if len(bars) < window:
        return None
    vals = [b.turnover for b in bars[-window:]]
    if all(v == 0 for v in vals):
        return None
    return sum(vals) / window


def uptrend(bars: Sequence[Bar], fast: int = 50, slow: int = 200) -> bool | None:
    """多头排列: 收盘 > MA(fast) > MA(slow)。数据不足返回 None。"""
    cl = closes(bars)
    ma_fast = sma(cl, fast)
    ma_slow = sma(cl, slow)
    if ma_fast is None or ma_slow is None:
        return None
    return cl[-1] > ma_fast > ma_slow


def above_ma(bars: Sequence[Bar], window: int) -> bool | None:
    cl = closes(bars)
    ma = sma(cl, window)
    if ma is None:
        return None
    return cl[-1] > ma


def pullback_depth(bars: Sequence[Bar], lookback: int = 60) -> float | None:
    """自近 lookback 日阶段高点的回撤幅度。

    融合框架 L3 买点条件 2: 正常回调应在 8%~25% 之间;
    >30% 属于欧奈尔所说的"弱者预警"。
    """
    if len(bars) < 2:
        return None
    span = bars[-lookback:] if len(bars) >= lookback else bars
    peak = max(b.high for b in span)
    if peak <= 0:
        return None
    return (peak - bars[-1].close) / peak


def broke_platform_high(bars: Sequence[Bar], lookback: int = 20) -> bool | None:
    """今日收盘是否突破了前 lookback 日(不含今日)的最高价。"""
    if len(bars) < lookback + 1:
        return None
    prior_high = max(b.high for b in bars[-lookback - 1 : -1])
    return bars[-1].close > prior_high


def broke_ma_on_volume(
    bars: Sequence[Bar], ma_window: int = 50, vol_mult: float = 1.4, grace: int = 5
) -> bool | None:
    """是否放量跌破 MA 且在 grace 个交易日内没有收回。

    对应卖出触发表的第二条。
    """
    if len(bars) < ma_window + grace + 1:
        return None
    cl = closes(bars)
    # 在最近 grace+1 天里找放量破位日
    for i in range(len(bars) - grace - 1, len(bars)):
        window_closes = cl[: i + 1]
        ma = sma(window_closes, ma_window)
        if ma is None:
            continue
        if cl[i] >= ma:
            continue
        base = sma(volumes(bars[:i]), 20)
        if not base or bars[i].volume < base * vol_mult:
            continue
        # 找到了破位日, 检查此后是否收回
        recovered = False
        for j in range(i + 1, len(bars)):
            ma_j = sma(cl[: j + 1], ma_window)
            if ma_j is not None and cl[j] > ma_j:
                recovered = True
                break
        if not recovered:
            return True
    return False


def percentile_rank(value: float, population: Sequence[float]) -> float | None:
    """value 在 population 中的百分位 (0~100)。

    对应 L 因子的 RS 评级: 欧奈尔的 1~99 分制本质就是全市场涨幅百分位。
    """
    vals = [v for v in population if v is not None]
    if not vals:
        return None
    below = sum(1 for v in vals if v < value)
    equal = sum(1 for v in vals if v == value)
    return 100.0 * (below + 0.5 * equal) / len(vals)


def rs_ratings(returns: dict[str, float | None]) -> dict[str, float]:
    """把 {代码: 区间收益} 批量转成 {代码: RS 百分位}。None 值不参与排名。"""
    population = [v for v in returns.values() if v is not None]
    out: dict[str, float] = {}
    for code, ret in returns.items():
        if ret is None:
            continue
        rank = percentile_rank(ret, population)
        if rank is not None:
            out[code] = rank
    return out


def distribution_days(bars: Sequence[Bar], window: int = 25, drop_pct: float = 0.002) -> int | None:
    """派发日计数 (欧奈尔 M 因子的核心可计算规则)。

    定义: 当日收跌幅度 >= drop_pct 且当日成交量 > 前一日成交量。
    统计最近 window 个交易日内的出现次数。
    """
    if len(bars) < window + 1:
        return None
    count = 0
    for i in range(len(bars) - window, len(bars)):
        prev, cur = bars[i - 1], bars[i]
        if prev.close <= 0:
            continue
        change = (cur.close - prev.close) / prev.close
        if change <= -drop_pct and cur.volume > prev.volume:
            count += 1
    return count


def gain_within(bars: Sequence[Bar], days: int) -> float | None:
    """近 days 个交易日的涨幅, 用于"3 周内涨 20% 须持满 8 周"规则。"""
    return trailing_return(bars, days)
