"""L3 买点、仓位与卖出规则引擎。

这一层实现融合框架里对两个源头冲突的处理:
- 冲突 A (买回调 vs 买突破): 回调本身不是买入信号, 回调后的重新起涨才是。
- 冲突 B (死拿 vs 20% 止盈): 不设固定止盈线, 改用卖出触发条件。

欧奈尔第 20 章第 13 条: "写下你的卖出法则, 以决定什么时候卖出。"
本模块就是那份写下来的法则。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

from . import fundamentals as fu
from .indicators import (
    Bar,
    broke_ma_on_volume,
    broke_platform_high,
    gain_within,
    percentile_rank,
    pullback_depth,
    trailing_return,
    volume_ratio,
)
from .market import MarketState

# --- 买点参数 ---
PULLBACK_MIN = 0.08
PULLBACK_MAX = 0.25
TRIGGER_VOLUME_RATIO = 1.4

# --- 分批建仓 ---
BATCH_WEIGHTS = (0.40, 0.35, 0.25)
SECOND_BATCH_GAIN = 0.02  # 相对首批成本 +2%~3% 才加第二批
THIRD_BATCH_NEEDS_BREAKOUT = True

# --- 仓位上限 ---
MAX_SINGLE_POSITION = 0.25
MAX_SECTOR_POSITION = 0.40
MAX_POSITIONS = 6
MIN_POSITIONS = 4
ALLOW_LEVERAGE = False

# --- 止损 ---
STOP_LOSS_NORMAL = 0.08
STOP_LOSS_DEFENSE = 0.03

# --- 持盈 ---
FAST_GAIN_DAYS = 15  # 约 3 周
FAST_GAIN_THRESHOLD = 0.20
FAST_GAIN_HOLD_DAYS = 40  # 约 8 周
RS_EXIT_LEVEL = 60.0


class Action(str, Enum):
    BUY = "BUY"
    WATCH = "WATCH"
    REJECT = "REJECT"


class SellAction(str, Enum):
    HOLD = "HOLD"
    TRIM_HALF = "TRIM_HALF"
    EXIT = "EXIT"
    STOP_OUT = "STOP_OUT"


@dataclass
class EntrySignal:
    thscode: str
    action: Action
    reasons: list[str] = field(default_factory=list)
    pullback: float | None = None
    volume_ratio: float | None = None
    broke_platform: bool | None = None
    stop_loss_price: float | None = None
    first_batch_weight: float = BATCH_WEIGHTS[0]


def evaluate_entry(
    thscode: str,
    bars: Sequence[Bar],
    market_state: MarketState,
    above_ma50: bool | None = None,
) -> EntrySignal:
    """判断是否可以下第一批。

    三个条件必须同时成立 (docs/03-融合投资框架.md L3):
      1. 趋势未破: 仍在 50 日线上方或刚回踩未有效跌破
      2. 回调幅度正常: 自阶段高点回撤 8%~25%
      3. 起涨确认: 放量上攻日, 或突破回调平台高点
    """
    sig = EntrySignal(thscode=thscode, action=Action.REJECT)

    if market_state is not MarketState.OFFENSE:
        sig.reasons.append(f"大盘 M 状态为 {market_state.value}, 不开新仓")
        return sig

    if len(bars) < 60:
        sig.reasons.append("K 线不足 60 根, 无法判断回调结构")
        return sig

    depth = pullback_depth(bars, 60)
    vr = volume_ratio(bars, 20)
    broke = broke_platform_high(bars, 20)
    sig.pullback, sig.volume_ratio, sig.broke_platform = depth, vr, broke

    if above_ma50 is False:
        sig.reasons.append("已有效跌破 50 日线, 趋势条件不成立")
        return sig

    if depth is None:
        sig.reasons.append("无法计算回调幅度")
        return sig
    if depth > PULLBACK_MAX:
        sig.reasons.append(f"自阶段高点回撤 {depth:.1%}, 超过 {PULLBACK_MAX:.0%}, 属于弱者")
        return sig

    confirmed = bool((vr is not None and vr >= TRIGGER_VOLUME_RATIO) or broke)
    if depth < PULLBACK_MIN and not broke:
        sig.action = Action.WATCH
        sig.reasons.append(f"回撤仅 {depth:.1%}, 尚未进入 {PULLBACK_MIN:.0%} 的分批区间")
        return sig

    if not confirmed:
        sig.action = Action.WATCH
        sig.reasons.append(
            f"回撤 {depth:.1%} 在区间内, 但未出现起涨确认 "
            f"(量比 {vr if vr is None else round(vr, 2)}, 未破平台高点), 只入观察池"
        )
        return sig

    sig.action = Action.BUY
    if vr is not None and vr >= TRIGGER_VOLUME_RATIO:
        sig.reasons.append(f"放量上攻确认, 量比 {vr:.2f}x")
    if broke:
        sig.reasons.append("突破回调平台高点")
    sig.reasons.append(f"回撤 {depth:.1%} 处于 {PULLBACK_MIN:.0%}~{PULLBACK_MAX:.0%} 区间")
    sig.stop_loss_price = round(bars[-1].close * (1 - STOP_LOSS_NORMAL), 4)
    sig.reasons.append(f"首批止损价 {sig.stop_loss_price} (成本 -{STOP_LOSS_NORMAL:.0%})")
    return sig


@dataclass
class Position:
    thscode: str
    sector: str
    batches: list[tuple[float, float]] = field(default_factory=list)  # (买入价, 权重)
    held_days: int = 0
    fast_gain_locked_until: int = 0  # 还需锁定持有的天数

    @property
    def weight(self) -> float:
        return sum(w for _, w in self.batches)

    @property
    def avg_cost(self) -> float | None:
        total_w = self.weight
        if total_w <= 0:
            return None
        return sum(p * w for p, w in self.batches) / total_w


def next_batch(
    position: Position, last_close: float, bars: Sequence[Bar], above_ma50: bool | None
) -> tuple[bool, str]:
    """能否加下一批。绝不向下加仓 —— 两个源头唯一都用"无例外"措辞的地方。"""
    filled = len(position.batches)
    if filled == 0:
        return True, "首批"
    if filled >= len(BATCH_WEIGHTS):
        return False, "三批已满"

    first_price = position.batches[0][0]
    if last_close <= first_price:
        return False, "现价不高于首批成本, 禁止向下加仓"

    if filled == 1:
        gain = (last_close - first_price) / first_price
        if gain < SECOND_BATCH_GAIN:
            return False, f"较首批成本仅 {gain:+.1%}, 未达 {SECOND_BATCH_GAIN:.0%} 加仓线"
        if above_ma50 is False:
            return False, "已跌破 50 日线, 不加仓"
        return True, f"较首批成本 {gain:+.1%} 且未破 50 日线"

    if THIRD_BATCH_NEEDS_BREAKOUT:
        broke = broke_platform_high(bars, 20)
        vr = volume_ratio(bars, 20)
        if broke and vr is not None and vr >= TRIGGER_VOLUME_RATIO:
            return True, f"突破前高并放量 (量比 {vr:.2f}x)"
        return False, "第三批需要突破前高并放量"
    return True, "第三批"


def stop_loss_price(entry_price: float, market_state: MarketState) -> float:
    """止损基准是每一批的买入价, 不是持仓均价。"""
    pct = STOP_LOSS_DEFENSE if market_state is MarketState.DEFENSE else STOP_LOSS_NORMAL
    return round(entry_price * (1 - pct), 4)


@dataclass
class ExitSignal:
    thscode: str
    action: SellAction
    reasons: list[str] = field(default_factory=list)


def evaluate_exit(
    thscode: str,
    bars: Sequence[Bar],
    position: Position,
    market_state: MarketState,
    quarterly_income: Sequence[dict] | None = None,
    market_returns_250d: Sequence[float] | None = None,
    as_of_ms: int | None = None,
) -> ExitSignal:
    """卖出触发表。没有固定止盈线, 只有触发条件。"""
    sig = ExitSignal(thscode=thscode, action=SellAction.HOLD)
    if not bars:
        sig.reasons.append("无行情数据")
        return sig

    last = bars[-1].close

    # 1. 止损优先于一切
    for i, (price, _w) in enumerate(position.batches, start=1):
        stop = stop_loss_price(price, market_state)
        if last <= stop:
            sig.action = SellAction.STOP_OUT
            sig.reasons.append(
                f"第 {i} 批买入价 {price} 的止损位 {stop} 已被击穿 (现价 {last}), 立即清掉该笔"
            )
            return sig

    # 2. 快速拉升锁定期: 3 周内涨 20% 必须持满 8 周
    fast = gain_within(bars, FAST_GAIN_DAYS)
    in_lock = fast is not None and fast >= FAST_GAIN_THRESHOLD and position.held_days < FAST_GAIN_HOLD_DAYS
    if in_lock:
        sig.reasons.append(
            f"近 {FAST_GAIN_DAYS} 日涨 {fast:.1%}, 触发欧奈尔 8 周锁定规则, "
            f"已持有 {position.held_days} 日, 除止损外不卖"
        )
        return sig

    # 3. 基本面转弱
    if quarterly_income:
        series = fu.single_quarter_series(quarterly_income, as_of_ms)
        if fu.decelerating_two_quarters(series, "net_profit"):
            sig.action = SellAction.EXIT
            sig.reasons.append("单季净利增速连续两季回落 >= 1/3, 清仓")
            return sig
        if fu.decelerating_two_quarters(series, "revenue"):
            sig.action = SellAction.EXIT
            sig.reasons.append("单季营收增速连续两季回落 >= 1/3, 清仓")
            return sig

    # 4. 放量跌破 50 日线且未收回
    if broke_ma_on_volume(bars, 50, TRIGGER_VOLUME_RATIO, grace=5):
        sig.action = SellAction.EXIT
        sig.reasons.append("放量跌破 50 日线且 5 日内未收回, 清仓")
        return sig

    # 5. RS 掉队
    if market_returns_250d:
        ret = trailing_return(bars, 250)
        if ret is not None:
            rs = percentile_rank(ret, market_returns_250d)
            if rs is not None and rs < RS_EXIT_LEVEL:
                sig.action = SellAction.EXIT
                sig.reasons.append(f"RS 百分位跌至 {rs:.0f} (< {RS_EXIT_LEVEL:.0f}), 已非领军股, 清仓")
                return sig

    # 6. 大盘转防守
    if market_state is MarketState.DEFENSE:
        sig.action = SellAction.TRIM_HALF
        sig.reasons.append("大盘 M 状态转 DEFENSE, 至少减半")
        return sig

    sig.reasons.append("未触发任何卖出条件, 继续持有")
    return sig


@dataclass
class PortfolioCheck:
    ok: bool
    violations: list[str] = field(default_factory=list)


def check_portfolio(
    positions: Sequence[Position], market_state: MarketState, leverage: float = 1.0
) -> PortfolioCheck:
    """组合层面的硬约束检查。"""
    violations: list[str] = []

    if not ALLOW_LEVERAGE and leverage > 1.0:
        violations.append(f"使用了 {leverage:.2f}x 杠杆, 框架禁止加杠杆")

    total = sum(p.weight for p in positions)
    cap = market_state.max_exposure
    if total > cap + 1e-9:
        violations.append(f"总仓位 {total:.0%} 超过 {market_state.value} 状态上限 {cap:.0%}")

    for p in positions:
        if p.weight > MAX_SINGLE_POSITION + 1e-9:
            violations.append(f"{p.thscode} 单票仓位 {p.weight:.0%} 超过 {MAX_SINGLE_POSITION:.0%}")

    by_sector: dict[str, float] = {}
    for p in positions:
        by_sector[p.sector] = by_sector.get(p.sector, 0.0) + p.weight
    for sector, w in by_sector.items():
        if w > MAX_SECTOR_POSITION + 1e-9:
            violations.append(f"板块「{sector}」合计 {w:.0%} 超过 {MAX_SECTOR_POSITION:.0%}")

    held = [p for p in positions if p.weight > 0]
    if len(held) > MAX_POSITIONS:
        violations.append(f"持股 {len(held)} 只, 超过上限 {MAX_POSITIONS} 只")

    return PortfolioCheck(ok=not violations, violations=violations)
