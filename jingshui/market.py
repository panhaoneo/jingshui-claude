"""L0 大盘闸门 (CAN SLIM 的 M)。

欧奈尔: "你可能已经符合了之前 6 章中提到的所有要素, 但如果错误地估计了
市场的走势, 手里 3/4 的股票会随着大盘狂跌。"
静水2008: "顺势而为"。

两者说的是同一件事, 这里用派发日计数把它变成可计算的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence

from .indicators import Bar, closes, distribution_days, sma

# 默认跟踪的宽基指数
DEFAULT_INDEXES = {
    "000300.SH": "沪深300",
    "399006.SZ": "创业板指",
    "000001.SH": "上证综指",
}


class MarketState(str, Enum):
    OFFENSE = "OFFENSE"
    CAUTION = "CAUTION"
    DEFENSE = "DEFENSE"

    @property
    def max_exposure(self) -> float:
        """该状态下允许的最高总仓位。"""
        return {"OFFENSE": 1.0, "CAUTION": 0.5, "DEFENSE": 0.2}[self.value]

    @property
    def rank(self) -> int:
        """越大越保守, 用于在多个指数间取最保守值。"""
        return {"OFFENSE": 0, "CAUTION": 1, "DEFENSE": 2}[self.value]


@dataclass
class IndexVerdict:
    thscode: str
    name: str
    state: MarketState
    distribution_days: int | None
    uptrend: bool | None
    above_ma50: bool | None
    ma50_above_ma200: bool | None
    reasons: list[str] = field(default_factory=list)


@dataclass
class MarketVerdict:
    state: MarketState
    max_exposure: float
    stop_loss_pct: float
    per_index: list[IndexVerdict]

    @property
    def can_open_new(self) -> bool:
        return self.state is MarketState.OFFENSE


def judge_index(
    thscode: str,
    name: str,
    bars: Sequence[Bar],
    window: int = 25,
    offense_max_dist: int = 2,
    defense_min_dist: int = 5,
) -> IndexVerdict:
    """对单个指数判定 M 状态。

    OFFENSE: 多头排列 (收盘 > MA50 > MA200) 且近 window 日派发日 <= 2
    DEFENSE: 跌破 MA50 且派发日 >= 5, 或 MA50 已下穿 MA200
    CAUTION: 其余
    """
    cl = closes(bars)
    ma50 = sma(cl, 50)
    ma200 = sma(cl, 200)
    dist = distribution_days(bars, window=window)

    above50 = None if ma50 is None else cl[-1] > ma50
    ma_stack = None if (ma50 is None or ma200 is None) else ma50 > ma200
    up = None if (above50 is None or ma_stack is None) else (above50 and ma_stack)

    reasons: list[str] = []
    if ma50 is None or ma200 is None:
        reasons.append("均线数据不足 (需要至少 200 根日 K), 保守判定为 CAUTION")
        state = MarketState.CAUTION
    elif ma_stack is False:
        reasons.append("MA50 已下穿 MA200, 中期趋势转空")
        state = MarketState.DEFENSE
    elif above50 is False and dist is not None and dist >= defense_min_dist:
        reasons.append(f"跌破 MA50 且近 {window} 日派发日 {dist} 次 (>= {defense_min_dist})")
        state = MarketState.DEFENSE
    elif up and dist is not None and dist <= offense_max_dist:
        reasons.append(f"多头排列成立, 近 {window} 日派发日仅 {dist} 次")
        state = MarketState.OFFENSE
    else:
        if dist is not None:
            reasons.append(f"近 {window} 日派发日 {dist} 次, 趋势未确认为进攻")
        else:
            reasons.append("派发日数据不足")
        state = MarketState.CAUTION

    return IndexVerdict(
        thscode=thscode,
        name=name,
        state=state,
        distribution_days=dist,
        uptrend=up,
        above_ma50=above50,
        ma50_above_ma200=ma_stack,
        reasons=reasons,
    )


def judge_market(
    index_bars: Mapping[str, Sequence[Bar]],
    names: Mapping[str, str] | None = None,
) -> MarketVerdict:
    """对多个指数分别判定, 取最保守的一个作为全局状态。"""
    names = names or DEFAULT_INDEXES
    verdicts = [
        judge_index(code, names.get(code, code), bars)
        for code, bars in index_bars.items()
    ]
    if not verdicts:
        raise ValueError("至少需要一个指数的 K 线序列")
    worst = max(verdicts, key=lambda v: v.state.rank).state
    # 熊市中欧奈尔把止损从 7%~8% 收紧到 3%
    stop = 0.03 if worst is MarketState.DEFENSE else 0.08
    return MarketVerdict(
        state=worst,
        max_exposure=worst.max_exposure,
        stop_loss_pct=stop,
        per_index=verdicts,
    )
