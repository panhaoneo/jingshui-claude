"""L2 个股六因子评分 (CAN SLIM 的 A 股实现)。

输入是已经取好的原始数据, 本模块不发网络请求, 因此可以完全离线测试。
每个因子都返回得分和可读理由, 便于人工复核 —— 欧奈尔第 20 章第 21 条:
"研读你持有其股票的公司, 了解它的故事。"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from . import fundamentals as fu
from .indicators import (
    Bar,
    avg_turnover,
    distance_from_high,
    trailing_return,
    percentile_rank,
    volume_ratio,
)

# --- 硬性排除门槛 (docs/03-融合投资框架.md L2) ---
MIN_LISTED_DAYS = 250
MIN_AVG_TURNOVER = 5e7  # 近 20 日日均成交额下限, 5000 万元
MAX_DISTANCE_FROM_HIGH = 0.30  # 距 250 日最高价超过 30% 视为弱者

# --- 因子门槛 ---
C_MIN_GROWTH = 0.25
C_FULL_GROWTH = 0.50
C_MIN_REVENUE_GROWTH = 0.25
A_MIN_ANNUAL_GROWTH = 0.25
A_MIN_ROE = 0.17
A_FULL_ROE = 0.25
N_FULL_DISTANCE = 0.05
N_HALF_DISTANCE = 0.10
S_MIN_VOLUME_RATIO = 1.4
L_MIN_RS = 80.0
L_FULL_RS = 90.0

PASS_SCORE = 70.0
REQUIRED_NONZERO = ("C", "A", "N", "L")


@dataclass
class StockInput:
    """一只候选股的全部输入。"""

    thscode: str
    name: str
    bars: Sequence[Bar]
    quarterly_income: Sequence[dict]
    annual_income: Sequence[dict]
    annual_balance: Sequence[dict]
    market_returns_250d: Sequence[float]
    sector_returns_250d: Sequence[float] = field(default_factory=list)
    org_net_value_60d: float | None = None
    is_st: bool = False
    as_of_ms: int | None = None


@dataclass
class FactorResult:
    code: str
    score: float
    max_score: float
    reasons: list[str] = field(default_factory=list)

    @property
    def zero(self) -> bool:
        return self.score <= 0


@dataclass
class StockScore:
    thscode: str
    name: str
    total: float
    factors: dict[str, FactorResult]
    excluded: bool
    exclusion_reasons: list[str] = field(default_factory=list)
    metrics: dict[str, float | None] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        if self.excluded or self.total < PASS_SCORE:
            return False
        return all(not self.factors[c].zero for c in REQUIRED_NONZERO if c in self.factors)


def _lerp_score(value: float, lo: float, hi: float, max_score: float) -> float:
    """value 从 lo 线性升到 hi 时得分从 0 升到 max_score, 超过 hi 封顶。"""
    if value < lo:
        return 0.0
    if hi <= lo:
        return max_score
    return max_score * min(1.0, (value - lo) / (hi - lo))


def check_exclusions(inp: StockInput) -> list[str]:
    """硬性排除。任何一条命中就不再进入评分。"""
    reasons: list[str] = []
    if inp.is_st:
        reasons.append("ST / *ST 股")
    if len(inp.bars) < MIN_LISTED_DAYS:
        reasons.append(f"上市不足 {MIN_LISTED_DAYS} 个交易日, 无法计算 RS 与年度增长")
    turnover = avg_turnover(inp.bars, 20)
    if turnover is not None and turnover < MIN_AVG_TURNOVER:
        reasons.append(f"近 20 日日均成交额 {turnover/1e8:.2f} 亿元, 低于门槛, 无法分批进出")
    dfh = distance_from_high(inp.bars, 250)
    if dfh is not None and dfh > MAX_DISTANCE_FROM_HIGH:
        reasons.append(f"距 250 日最高价 {dfh:.1%}, 超过 {MAX_DISTANCE_FROM_HIGH:.0%} 的弱者预警线")

    series = fu.single_quarter_series(inp.quarterly_income, inp.as_of_ms)
    if series and series[-1].net_profit is not None and series[-1].net_profit < 0:
        reasons.append("最近一期单季归母净利润为负")
    return reasons


def score_c(inp: StockInput) -> FactorResult:
    """C = 当季每股收益与营收。满分 20。"""
    res = FactorResult("C", 0.0, 20.0)
    series = fu.single_quarter_series(inp.quarterly_income, inp.as_of_ms)
    if len(series) < 5:
        res.reasons.append("单季序列不足 5 期, 无法做同比")
        return res

    profit_yoy = fu.yoy(series, "net_profit")
    rev_yoy = fu.yoy(series, "revenue")

    if profit_yoy is None:
        res.reasons.append("单季归母净利同比不可比 (去年同期缺失或基数非正)")
    else:
        res.score += _lerp_score(profit_yoy, C_MIN_GROWTH, C_FULL_GROWTH, 12.0)
        res.reasons.append(f"单季归母净利同比 {profit_yoy:+.1%}")

    if rev_yoy is None:
        res.reasons.append("单季营收同比不可比")
    else:
        res.score += _lerp_score(rev_yoy, C_MIN_REVENUE_GROWTH, 0.50, 5.0)
        res.reasons.append(f"单季营收同比 {rev_yoy:+.1%}")

    accel = fu.is_accelerating(series, "net_profit")
    if accel:
        res.score += 3.0
        res.reasons.append("增速环比加速 (收益奇迹)")
    elif accel is False:
        res.reasons.append("增速未加速")

    if fu.decelerating_two_quarters(series):
        res.score = 0.0
        res.reasons.append("连续两季增速大幅回落, C 因子清零")

    res.score = min(res.score, res.max_score)
    return res


def score_a(inp: StockInput) -> FactorResult:
    """A = 年度收益增长率与 ROE。满分 15。"""
    res = FactorResult("A", 0.0, 15.0)
    hits, growths = fu.annual_growth_streak(
        inp.annual_income, threshold=A_MIN_ANNUAL_GROWTH, years=3, as_of_ms=inp.as_of_ms
    )
    shown = ", ".join("n/a" if g is None else f"{g:+.1%}" for g in growths) or "无数据"
    res.reasons.append(f"近 3 年年度归母净利增速: {shown}")
    res.score += 9.0 * hits / 3.0

    r = fu.roe(inp.annual_income, inp.annual_balance, inp.as_of_ms)
    if r is None:
        res.reasons.append("ROE 无法计算")
    else:
        res.score += _lerp_score(r, A_MIN_ROE, A_FULL_ROE, 6.0)
        res.reasons.append(f"ROE {r:.1%} (口径含少数股东权益, 略偏低)")

    res.score = min(res.score, res.max_score)
    return res


def score_n(inp: StockInput) -> FactorResult:
    """N = 股价新高。满分 20。

    欧奈尔全书最反直觉的一条: 领军股是在接近或创新高时才开始大涨的。
    """
    res = FactorResult("N", 0.0, 20.0)
    dfh = distance_from_high(inp.bars, 250)
    if dfh is None:
        res.reasons.append("K 线不足, 无法计算新高距离")
        return res
    if dfh <= N_FULL_DISTANCE:
        res.score = 20.0
    elif dfh <= N_HALF_DISTANCE:
        res.score = 10.0
    else:
        res.score = 0.0
    res.reasons.append(f"距 250 日最高价 {dfh:.1%}")
    return res


def score_s(inp: StockInput) -> FactorResult:
    """S = 供给与需求 (量能)。满分 10。"""
    res = FactorResult("S", 0.0, 10.0)
    vr = volume_ratio(inp.bars, 20)
    if vr is None:
        res.reasons.append("成交量数据不足")
    else:
        res.score += _lerp_score(vr, 1.0, S_MIN_VOLUME_RATIO, 7.0)
        res.reasons.append(f"当日量比 {vr:.2f}x (突破需 >= {S_MIN_VOLUME_RATIO}x)")

    turnover = avg_turnover(inp.bars, 20)
    if turnover is not None:
        res.score += _lerp_score(turnover, MIN_AVG_TURNOVER, 5e8, 3.0)
        res.reasons.append(f"近 20 日日均成交额 {turnover/1e8:.2f} 亿元")

    res.score = min(res.score, res.max_score)
    return res


def score_l(inp: StockInput) -> FactorResult:
    """L = 领军股还是拖油瓶。满分 20。

    欧奈尔: 大牛股启动前 RS 平均 87, 不买 RS 在 40~60 的滞后股。
    """
    res = FactorResult("L", 0.0, 20.0)
    ret = trailing_return(inp.bars, 250)
    if ret is None:
        res.reasons.append("不足 250 个交易日, 无法计算 RS")
        return res

    rs = percentile_rank(ret, inp.market_returns_250d)
    if rs is None:
        res.reasons.append("全市场收益样本为空, 无法排名")
        return res
    res.score += _lerp_score(rs, L_MIN_RS, L_FULL_RS, 14.0)
    res.reasons.append(f"近 250 日涨幅 {ret:+.1%}, 全市场 RS 百分位 {rs:.0f}")

    if rs < L_MIN_RS:
        # 欧奈尔的 RS >= 80 是绝对门槛: "不要购入股价相对强度在 40 多、50 多
        # 或 60 多的股票"。板块内排名只是加分项, 不能把滞后股捞进来。
        res.reasons.append(f"RS 未达 {L_MIN_RS:.0f}, 属滞后股, 不计板块内排名加分")
        return res

    if inp.sector_returns_250d:
        sector_rank = percentile_rank(ret, inp.sector_returns_250d)
        if sector_rank is not None:
            res.reasons.append(f"板块内百分位 {sector_rank:.0f}")
            if sector_rank >= 80:
                res.score += 6.0
            elif sector_rank >= 60:
                res.score += 3.0

    res.score = min(res.score, res.max_score)
    return res


def score_i(inp: StockInput) -> FactorResult:
    """I = 机构认同度。满分 10。

    重要: 契约不提供"持有该股的机构家数", 这里用龙虎榜机构净额和
    成交额作为代理变量, 是近似而非等价。因此权重设为六因子中最低。
    """
    res = FactorResult("I", 0.0, 10.0)
    if inp.org_net_value_60d is None:
        res.reasons.append("近 60 日无龙虎榜机构数据 (未上榜不等于无机构参与)")
    elif inp.org_net_value_60d > 0:
        res.score += 6.0
        res.reasons.append(f"近 60 日龙虎榜机构净买入 {inp.org_net_value_60d/1e8:+.2f} 亿元")
    else:
        res.reasons.append(f"近 60 日龙虎榜机构净卖出 {inp.org_net_value_60d/1e8:+.2f} 亿元")

    turnover = avg_turnover(inp.bars, 20)
    if turnover is not None:
        res.score += _lerp_score(turnover, 1e8, 1e9, 4.0)

    res.score = min(res.score, res.max_score)
    return res


def score_stock(inp: StockInput) -> StockScore:
    """跑完六因子, 返回带明细的评分。"""
    exclusions = check_exclusions(inp)
    if exclusions:
        return StockScore(
            thscode=inp.thscode,
            name=inp.name,
            total=0.0,
            factors={},
            excluded=True,
            exclusion_reasons=exclusions,
        )

    factors = {
        "C": score_c(inp),
        "A": score_a(inp),
        "N": score_n(inp),
        "S": score_s(inp),
        "L": score_l(inp),
        "I": score_i(inp),
    }
    total = round(sum(f.score for f in factors.values()), 2)

    series = fu.single_quarter_series(inp.quarterly_income, inp.as_of_ms)
    metrics = {
        "profit_yoy": fu.yoy(series, "net_profit"),
        "revenue_yoy": fu.yoy(series, "revenue"),
        "roe": fu.roe(inp.annual_income, inp.annual_balance, inp.as_of_ms),
        "distance_from_high": distance_from_high(inp.bars, 250),
        "return_250d": trailing_return(inp.bars, 250),
        "volume_ratio": volume_ratio(inp.bars, 20),
        "avg_turnover_20d": avg_turnover(inp.bars, 20),
        "net_margin": fu.net_margin(series),
    }

    return StockScore(
        thscode=inp.thscode,
        name=inp.name,
        total=total,
        factors=factors,
        excluded=False,
        metrics=metrics,
    )


def rank_candidates(scores: Sequence[StockScore]) -> list[StockScore]:
    """按总分降序返回通过筛选的候选。"""
    passed = [s for s in scores if s.passed]
    passed.sort(key=lambda s: s.total, reverse=True)
    return passed
