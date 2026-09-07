"""L1 行业景气度筛选。

静水2008 的第一个问句: "当下这个市场, 哪个行业业绩在批量超预期、
社会讨论度最高、政策明显倾斜、且行业指数已创新高?"
欧奈尔第 20 章第 7 条: "标的应位于行业排名前 10%"。

数据能给到的是后半句 —— 板块指数的相对强度和新高状态。
"业绩批量超预期"由 L2 在成分股层面间接验证。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .indicators import Bar, closes, distance_from_high, percentile_rank, sma, trailing_return


@dataclass
class SectorScore:
    thscode: str
    name: str
    score: float
    ret_120d: float | None
    ret_60d: float | None
    rs_120d: float | None
    rs_60d: float | None
    distance_from_high: float | None
    healthy_trend: bool | None

    @property
    def at_new_high(self) -> bool:
        return self.distance_from_high is not None and self.distance_from_high <= 0.02


def _trend_healthy(bars: Sequence[Bar]) -> bool | None:
    cl = closes(bars)
    ma50 = sma(cl, 50)
    ma200 = sma(cl, 200)
    if ma50 is None or ma200 is None:
        return None
    return cl[-1] > ma50 and ma50 > ma200


def score_sectors(
    sector_bars: Mapping[str, Sequence[Bar]],
    names: Mapping[str, str] | None = None,
) -> list[SectorScore]:
    """给每个板块打景气分 (0~100), 按分数降序返回。

    权重 (见 docs/03-融合投资框架.md L1):
      板块 RS(120日百分位) 40 / 创新高距离 30 / 中期动能(60日百分位) 20 / 趋势健康 10
    """
    names = names or {}
    ret120 = {c: trailing_return(b, 120) for c, b in sector_bars.items()}
    ret60 = {c: trailing_return(b, 60) for c, b in sector_bars.items()}
    pop120 = [v for v in ret120.values() if v is not None]
    pop60 = [v for v in ret60.values() if v is not None]

    out: list[SectorScore] = []
    for code, bars in sector_bars.items():
        r120, r60 = ret120[code], ret60[code]
        rs120 = percentile_rank(r120, pop120) if r120 is not None else None
        rs60 = percentile_rank(r60, pop60) if r60 is not None else None
        dfh = distance_from_high(bars, window=250)
        healthy = _trend_healthy(bars)

        score = 0.0
        if rs120 is not None:
            score += 40.0 * rs120 / 100.0
        if dfh is not None:
            # 距新高 0% 得满分, 距 10% 及以上得 0 分, 中间线性衰减
            score += 30.0 * max(0.0, 1.0 - dfh / 0.10)
        if rs60 is not None:
            score += 20.0 * rs60 / 100.0
        if healthy:
            score += 10.0

        out.append(
            SectorScore(
                thscode=code,
                name=names.get(code, code),
                score=round(score, 2),
                ret_120d=r120,
                ret_60d=r60,
                rs_120d=rs120,
                rs_60d=rs60,
                distance_from_high=dfh,
                healthy_trend=healthy,
            )
        )
    out.sort(key=lambda s: s.score, reverse=True)
    return out


def leading_sectors(
    scores: Sequence[SectorScore], top_n: int = 5, require_new_high: bool = True
) -> list[SectorScore]:
    """取主线板块。

    require_new_high 对应静水原文的硬条件 "这个行业的指数已经创下历史新高"。
    除了距新高 <=2%, 还必须趋势健康 (收盘 > MA50 > MA200) —— 一条完全横盘的
    指数在技术上也"位于 250 日最高价附近", 但它没有趋势, 不是主线。
    若开启后不足 top_n 个, 返回实际数量而不是放宽标准凑数。
    """
    if require_new_high:
        pool = [s for s in scores if s.at_new_high and s.healthy_trend]
    else:
        pool = list(scores)
    return pool[:top_n]
