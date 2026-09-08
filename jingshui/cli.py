"""命令行入口。

    python -m jingshui market      # L0 大盘闸门
    python -m jingshui sectors     # L1 行业景气排名
    python -m jingshui scan        # L0->L3 全流程, 结果落盘
    python -m jingshui explain     # 打印框架规则速查表
    python -m jingshui doctor      # 逐个探测端点可用性

需要环境变量 HITHINK_FINANCE_API_KEY (explain 子命令除外)。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .client import HithinkClient, MissingApiKey
from .pipeline import Pipeline, ScanConfig
from . import rules, screener


def _pipeline(args) -> Pipeline:
    client = HithinkClient()
    cfg = ScanConfig(
        lookback_days=args.lookback,
        sector_tag=args.tag,
        top_sectors=args.top,
        max_stocks_per_sector=args.per_sector,
        rs_reference_index=args.rs_reference,
        request_pause=args.request_pause,
        org_flow_days=args.org_flow_days,
        cache_dir=args.cache_dir,
    )
    return Pipeline(client, cfg)


def cmd_market(args) -> int:
    verdict = _pipeline(args).market_gate()
    print(f"M 状态: {verdict.state.value}")
    print(f"允许最高仓位: {verdict.max_exposure:.0%}")
    print(f"本状态止损线: {verdict.stop_loss_pct:.0%}")
    print()
    for v in verdict.per_index:
        dist = "n/a" if v.distribution_days is None else str(v.distribution_days)
        print(f"  {v.name} ({v.thscode}): {v.state.value}  近 25 日派发日 {dist}")
        for r in v.reasons:
            print(f"      - {r}")
    return 0


def cmd_sectors(args) -> int:
    pipe = _pipeline(args)
    scores = pipe.sector_scan()
    leaders = pipe.leading(scores)
    leader_codes = {s.thscode for s in leaders}
    print(f"共评分 {len(scores)} 个板块, 主线 {len(leaders)} 个 (标 * 者)\n")
    for s in scores[: args.top * 4]:
        mark = "*" if s.thscode in leader_codes else " "
        dfh = "n/a" if s.distance_from_high is None else f"{s.distance_from_high:.1%}"
        r120 = "n/a" if s.ret_120d is None else f"{s.ret_120d:+.1%}"
        print(f"{mark} {s.score:6.2f}  {s.name:<16} 距新高 {dfh:>7}  120日 {r120:>8}")
    if not leaders:
        print("\n没有板块处于新高附近, 当前无主线, 按框架不开新仓。")
    return 0


def cmd_scan(args) -> int:
    summary = _pipeline(args).run(out_dir=args.out)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_explain(_args) -> int:
    print(
        f"""景气 α 趋势跟随系统 —— 规则速查

L0 大盘闸门 (M)
  OFFENSE  多头排列 + 近 25 日派发日 <= 2   最高仓位 100%
  CAUTION  其余                             最高仓位  50%, 不开新仓
  DEFENSE  破 MA50 且派发日 >= 5 或死叉      最高仓位  20%, 止损收紧到 3%

L1 行业景气 (默认取前 5 个主线板块)
  板块 RS(120日) 40 分 / 距新高 30 分 / 60日动能 20 分 / 趋势健康 10 分
  硬条件: 板块指数须在 250 日新高附近 (<=2%)

L2 个股六因子 (满分 100, 入选线 {screener.PASS_SCORE:.0f})
  C 当季 20   单季归母净利同比 >= 25%, 营收 >= 25%, 增速加速加分
  A 年度 15   近 3 年年度净利每年 >= 25%, ROE >= 17%
  N 新高 20   距 250 日最高价 <= 5% 满分
  S 量能 10   起涨日量比 >= 1.4x, 日均成交额越大越好
  L 领军 20   全市场 RS 百分位 >= 80, 板块内前 20% 加分
  I 机构 10   龙虎榜机构净买入 (代理变量, 非机构持仓家数)
  必须 C/A/N/L 四项均不为 0

L3 买点与仓位
  买点三条件同时成立: 未破 50 日线 / 回撤 {rules.PULLBACK_MIN:.0%}~{rules.PULLBACK_MAX:.0%} / 起涨确认(量比 >= {rules.TRIGGER_VOLUME_RATIO}x 或破平台高)
  分批 {int(rules.BATCH_WEIGHTS[0]*100)}% + {int(rules.BATCH_WEIGHTS[1]*100)}% + {int(rules.BATCH_WEIGHTS[2]*100)}%, 绝不向下加仓
  单票上限 {rules.MAX_SINGLE_POSITION:.0%} / 单板块 {rules.MAX_SECTOR_POSITION:.0%} / 持股 {rules.MIN_POSITIONS}~{rules.MAX_POSITIONS} 只 / 禁止杠杆
  止损 {rules.STOP_LOSS_NORMAL:.0%} (DEFENSE 时 {rules.STOP_LOSS_DEFENSE:.0%}), 基准是每一批的买入价

卖出触发 (没有固定止盈线)
  单季净利或营收增速连续两季回落 >= 1/3      -> 清仓
  放量跌破 50 日线且 5 日内未收回            -> 清仓
  RS 百分位跌至 < {rules.RS_EXIT_LEVEL:.0f}                      -> 清仓
  大盘转 DEFENSE                             -> 至少减半
  首次触及 50 日线尚未跌破                   -> 不动
  近 {rules.FAST_GAIN_DAYS} 日涨 >= {rules.FAST_GAIN_THRESHOLD:.0%}                     -> 锁定持有至少 {rules.FAST_GAIN_HOLD_DAYS} 个交易日

本框架是研究工具, 不构成投资建议。详见 docs/03-融合投资框架.md。"""
    )
    return 0



def cmd_doctor(args, client=None) -> int:
    """逐个探测框架依赖的端点, 报告哪些可用。

    在受限网络里跑不通全流程时, 用它区分三种情况:
    Key 没配 / 域名被网络策略拦截 / 某个端点权限或参数有问题。
    """
    import time
    import urllib.error

    from .client import HithinkClient, HithinkError, HttpStatusError

    pause = getattr(args, "pause", 1.0) if args is not None else 0.0
    if client is None:
        try:
            # 保留少量重试: 限流是暂时的, 一次 429 不代表端点不可用
            client = HithinkClient(max_retries=2)
        except MissingApiKey as exc:
            print(f"[FAIL] API Key: {exc}")
            return 2
        print("[ OK ] API Key: 已加载 (来源: 环境变量或用户级凭据文件)")

    end = int(__import__("time").time() * 1000)
    start = end - 400 * 86_400_000

    probes = [
        ("标的检索", lambda: client.search_ticker("600519", limit=1)),
        ("行情快照", lambda: client.price_snapshot(["600519.SH"])),
        ("个股日线", lambda: client.price_history("600519.SH", start, end)),
        ("季度利润表", lambda: client.income_statements("600519.SH", "quarterly", 8)),
        ("年度利润表", lambda: client.income_statements("600519.SH", "annual", 5)),
        ("年度资产负债表", lambda: client.balance_sheets("600519.SH", "annual", 5)),
        ("行业指数目录", lambda: client.index_catalog("industry")),
        ("指数日线", lambda: client.index_history("000300.SH", start, end)),
        ("指数成分股", lambda: client.index_constituents("000300.SH")),
        ("估值快照", lambda: client.valuations(["600519.SH"])),
        ("龙虎榜机构榜", lambda: client.dragon_tiger("org")),
    ]

    failures = 0
    throttled = 0
    unreachable = 0
    for i, (label, call) in enumerate(probes):
        if i and pause:
            # 逐个探测本身就可能撞上限流, 主动放慢比事后解释更省事
            time.sleep(pause)
        try:
            result = call()
        except HithinkError as exc:
            failures += 1
            if exc.code == 4001:
                throttled += 1
            print(f"[FAIL] {label}: 上游返回 code={exc.code} {exc.message}")
        except HttpStatusError as exc:
            failures += 1
            if exc.status == 429:
                throttled += 1
                hint = "限流, 上游要求降低请求频率"
            elif exc.status in (401, 403):
                hint = "鉴权或访问策略拒绝, 检查 API Key 是否有效"
            else:
                hint = "网关或服务端错误"
            print(f"[FAIL] {label}: HTTP {exc.status} {exc.reason} -> {hint}")
        except urllib.error.URLError as exc:
            failures += 1
            unreachable += 1
            print(f"[FAIL] {label}: 网络不可达 ({exc.reason})")
        except Exception as exc:  # noqa: BLE001 - 自检要报告而不是崩掉
            failures += 1
            print(f"[FAIL] {label}: {type(exc).__name__}: {exc}")
        else:
            size = len(result) if isinstance(result, (list, dict)) else 1
            print(f"[ OK ] {label}: 返回 {size} 条")

    print()
    if not failures:
        print(f"全部 {len(probes)} 个端点可用, 可以直接跑 python -m jingshui scan。")
        return 0

    print(f"{len(probes) - failures}/{len(probes)} 个端点可用。")
    if throttled:
        print(
            f"其中 {throttled} 个是限流 (HTTP 429 或 code=4001), 不是端点不可用。"
            f"\n  重试: python -m jingshui doctor --pause {max(pause * 3, 3):.0f}"
            "\n  跑全流程时同样要放慢: python -m jingshui scan --request-pause 1.0"
        )
    if unreachable == len(probes):
        print("全部是网络不可达 -> 域名被网络策略或防火墙拦截, 换一台能直连的机器。")
    print("code=2003 -> 该能力未授权; code=1xxx -> 参数问题。")
    return 1


DEFAULTS = {
    "lookback": 500,
    "tag": "industry",
    "top": 5,
    "per_sector": 40,
    "rs_reference": None,
    "request_pause": 0.15,
    "pause": 1.0,
    "org_flow_days": 60,
    "cache_dir": ".cache/jingshui",
    "out": "output",
    "verbose": False,
}


class _Parser(argparse.ArgumentParser):
    """解析完成后再补默认值。

    不能用 p.set_defaults(): parents=[common] 让顶层和各子命令共享同一批
    action 对象, set_defaults 会顺手把 action.default 从 SUPPRESS 改成真实
    默认值, 于是子命令解析时又会覆盖掉子命令之前解析到的值。
    """

    def parse_args(self, args=None, namespace=None):  # type: ignore[override]
        parsed = super().parse_args(args, namespace)
        for key, value in DEFAULTS.items():
            if not hasattr(parsed, key):
                setattr(parsed, key, value)
        return parsed


def build_parser() -> argparse.ArgumentParser:
    """公共参数同时挂在顶层和每个子命令上, 两种写法都可用:

        jingshui doctor --pause 3
        jingshui --pause 3 doctor

    关键在于所有公共参数的 default 都是 argparse.SUPPRESS: 否则子命令解析时
    会用自己的默认值静默覆盖掉子命令之前已经解析到的值 (argparse 的已知陷阱,
    表现为参数被无声忽略)。真正的默认值在解析结束后由 _Parser 补齐。
    """
    common = argparse.ArgumentParser(add_help=False)
    add = common.add_argument
    sup = argparse.SUPPRESS
    add("--lookback", type=int, default=sup, help="回看的自然日天数 (默认 500)")
    add("--tag", default=sup, help="板块类别: industry / cn_concept (默认 industry)")
    add("--top", type=int, default=sup, help="主线板块数量 (默认 5)")
    add("--per-sector", type=int, default=sup, help="每个板块最多扫描的成分股数 (默认 40)")
    add("--rs-reference", default=sup,
        help="用于扩大 RS 样本总体的宽基指数, 如 000300.SH (会显著增加请求数)")
    add("--request-pause", type=float, default=sup,
        help="每次请求之间的最小间隔秒数, 撞限流时调大 (默认 0.15)")
    add("--pause", type=float, default=sup,
        help="doctor 每次探测之间的间隔秒数 (默认 1.0)")
    add("--org-flow-days", type=int, default=sup,
        help="龙虎榜回溯天数, 每天一次请求, 限流时可调小 (默认 60)")
    add("--cache-dir", default=sup, help="缓存目录 (默认 .cache/jingshui)")
    add("--out", default=sup, help="结果输出目录 (默认 output)")
    add("-v", "--verbose", action="store_true", default=sup)

    p = _Parser(prog="jingshui", description="景气 α 趋势跟随系统", parents=[common])
    sub = p.add_subparsers(dest="command", required=True)
    for name, help_text, func in [
        ("market", "L0 大盘闸门", cmd_market),
        ("sectors", "L1 行业景气排名", cmd_sectors),
        ("scan", "L0->L3 全流程", cmd_scan),
        ("explain", "打印规则速查表", cmd_explain),
        ("doctor", "逐个探测端点可用性", cmd_doctor),
    ]:
        sub.add_parser(name, help=help_text, parents=[common]).set_defaults(func=func)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except MissingApiKey as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
