"""财务因子层: C (当季) 与 A (年度)。

三个 A 股特有的处理, 缺一不可 (见 docs/03-融合投资框架.md L2 节):

1. 单季化: A 股定期报告是年初至今累计口径, 单季值 = 本期累计 - 上期累计。
2. 同比基准: 与去年同一季度比, 不与本年上一季度比。
3. 时点纪律: 用 report_date_ms (实际披露日) 判断"当时是否已知", 不用 period_end_ms。

`None` 一律表示"未披露或无法计算", 绝不补零。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Iterable, Sequence

# 单季化时用于识别季度序号的报告期末月份
_MONTH_TO_QUARTER = {3: 1, 6: 2, 9: 3, 12: 4}


def _ms_to_date(ms: int) -> _dt.date:
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).date()


def quarter_of(period_end_ms: int) -> int | None:
    """从报告期末时间戳推出季度序号 1~4。非标准月份返回 None。"""
    month = _ms_to_date(period_end_ms).month
    return _MONTH_TO_QUARTER.get(month)


@dataclass(frozen=True)
class QuarterPoint:
    """单季化之后的一期财务数据。"""

    fiscal_year: int
    quarter: int
    period_end_ms: int
    report_date_ms: int | None
    net_profit: float | None
    revenue: float | None
    eps: float | None

    @property
    def key(self) -> tuple[int, int]:
        return (self.fiscal_year, self.quarter)


def _num(row: dict, field: str) -> float | None:
    value = row.get(field)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_of_filter(rows: Iterable[dict], as_of_ms: int | None) -> list[dict]:
    """时点过滤: 只保留在 as_of_ms 之前已经披露的报告期。

    不做这一步, 任何回测都带未来函数。as_of_ms 为 None 时不过滤。
    缺少 report_date_ms 的行被丢弃 —— 无法证明当时可见的数据不能用。
    """
    rows = list(rows)
    if as_of_ms is None:
        return rows
    kept = []
    for row in rows:
        report_ms = row.get("report_date_ms")
        if report_ms is None:
            continue
        if int(report_ms) <= as_of_ms:
            kept.append(row)
    return kept


def single_quarter_series(
    cumulative_rows: Sequence[dict], as_of_ms: int | None = None
) -> list[QuarterPoint]:
    """把累计口径的季度利润表还原成单季序列, 按时间升序返回。

    Q1 的累计值本身就是单季值; Q2~Q4 需要减去同年上一期累计值。
    同年上一期缺失时, 该季返回 None 而不是用累计值冒充单季值。
    """
    rows = as_of_filter(cumulative_rows, as_of_ms)
    cumulative: dict[tuple[int, int], dict] = {}
    for row in rows:
        pe = row.get("period_end_ms")
        fy = row.get("fiscal_year")
        if pe is None or fy is None:
            continue
        q = quarter_of(int(pe))
        if q is None:
            continue
        cumulative[(int(fy), q)] = row

    out: list[QuarterPoint] = []
    for (fy, q), row in sorted(cumulative.items()):
        prev = cumulative.get((fy, q - 1)) if q > 1 else None

        def delta(field: str) -> float | None:
            cur = _num(row, field)
            if cur is None:
                return None
            if q == 1:
                return cur
            if prev is None:
                return None
            prev_val = _num(prev, field)
            if prev_val is None:
                return None
            return cur - prev_val

        out.append(
            QuarterPoint(
                fiscal_year=fy,
                quarter=q,
                period_end_ms=int(row["period_end_ms"]),
                report_date_ms=(
                    int(row["report_date_ms"])
                    if row.get("report_date_ms") is not None
                    else None
                ),
                net_profit=delta("parent_holder_net_profit"),
                revenue=delta("operating_income"),
                eps=delta("basic_eps"),
            )
        )
    return out


def _growth(new: float | None, old: float | None) -> float | None:
    """同比增速。基数 <= 0 时返回 None。

    亏损转盈利的增长率在数学上没有意义, 欧奈尔也明确要求剔除"从深坑回升"
    的伪成长, 所以这里直接判定为不可比, 而不是给一个虚高的正数。
    """
    if new is None or old is None or old <= 0:
        return None
    return (new - old) / old


def yoy(series: Sequence[QuarterPoint], field: str, back: int = 0) -> float | None:
    """倒数第 back+1 个季度相对其去年同季的同比增速。

    field 取 'net_profit' / 'revenue' / 'eps'。
    """
    if len(series) < back + 1:
        return None
    idx = len(series) - 1 - back
    cur = series[idx]
    lookup = {p.key: p for p in series}
    prior = lookup.get((cur.fiscal_year - 1, cur.quarter))
    if prior is None:
        return None
    return _growth(getattr(cur, field), getattr(prior, field))


def is_accelerating(series: Sequence[QuarterPoint], field: str = "net_profit") -> bool | None:
    """最近一季同比增速是否高于上一季同比增速 (欧奈尔的"收益奇迹")。"""
    latest = yoy(series, field, back=0)
    previous = yoy(series, field, back=1)
    if latest is None or previous is None:
        return None
    return latest > previous


def decelerating_two_quarters(
    series: Sequence[QuarterPoint], field: str = "net_profit", drop: float = 1 / 3
) -> bool | None:
    """连续两个季度增速大幅回落 —— 卖出触发条件之一。

    欧奈尔的判据是增速降幅达 2/3 (如 50%->15%); 这里用可配置的 drop,
    默认放宽到 1/3, 与融合框架卖出表一致。
    """
    g0 = yoy(series, field, back=0)
    g1 = yoy(series, field, back=1)
    g2 = yoy(series, field, back=2)
    if g0 is None or g1 is None or g2 is None:
        return None
    if g2 <= 0 or g1 <= 0:
        return None
    return (g1 < g2 * (1 - drop)) and (g0 < g1 * (1 - drop))


def annual_growth_streak(
    annual_rows: Sequence[dict],
    field: str = "parent_holder_net_profit",
    threshold: float = 0.25,
    years: int = 3,
    as_of_ms: int | None = None,
) -> tuple[int, list[float | None]]:
    """近 years 个完整年度里, 有几年同比增速达到 threshold。

    返回 (达标年数, 各年增速列表[由远及近])。
    对应 CAN SLIM 的 A: 欧奈尔要求近 3 年每年 >= 25%。
    """
    rows = as_of_filter(annual_rows, as_of_ms)
    by_year: dict[int, float | None] = {}
    for row in rows:
        fy = row.get("fiscal_year")
        if fy is None:
            continue
        by_year[int(fy)] = _num(row, field)
    ordered = sorted(by_year)
    growths: list[float | None] = []
    for fy in ordered[-years:]:
        growths.append(_growth(by_year.get(fy), by_year.get(fy - 1)))
    hits = sum(1 for g in growths if g is not None and g >= threshold)
    return hits, growths


def roe(
    income_rows: Sequence[dict],
    balance_rows: Sequence[dict],
    as_of_ms: int | None = None,
) -> float | None:
    """最近一个完整年度的 ROE = 归母净利润 / 所有者权益合计。

    近似说明: 契约只提供 `holder_equity_total` (含少数股东权益),
    没有单独的归母权益, 因此存在少数股东权益时本值会被低估。
    欧奈尔的门槛是 >= 17%, 使用时应知道这个口径偏差。
    """
    inc = as_of_filter(income_rows, as_of_ms)
    bal = as_of_filter(balance_rows, as_of_ms)
    if not inc or not bal:
        return None
    inc_by_year = {
        int(r["fiscal_year"]): r for r in inc if r.get("fiscal_year") is not None
    }
    bal_by_year = {
        int(r["fiscal_year"]): r for r in bal if r.get("fiscal_year") is not None
    }
    common = sorted(set(inc_by_year) & set(bal_by_year))
    if not common:
        return None
    year = common[-1]
    profit = _num(inc_by_year[year], "parent_holder_net_profit")
    equity = _num(bal_by_year[year], "holder_equity_total")
    if profit is None or equity is None or equity <= 0:
        return None
    return profit / equity


def net_margin(series: Sequence[QuarterPoint]) -> float | None:
    """最近单季税后净利率。欧奈尔要求它处于或接近历史高位。"""
    if not series:
        return None
    last = series[-1]
    if last.net_profit is None or not last.revenue or last.revenue <= 0:
        return None
    return last.net_profit / last.revenue


def margin_near_high(series: Sequence[QuarterPoint], lookback: int = 8) -> bool | None:
    """最近单季净利率是否达到近 lookback 季的最高值的 95% 以上。"""
    margins = []
    for p in series[-lookback:]:
        if p.net_profit is None or not p.revenue or p.revenue <= 0:
            continue
        margins.append(p.net_profit / p.revenue)
    if len(margins) < 2:
        return None
    return margins[-1] >= max(margins) * 0.95
