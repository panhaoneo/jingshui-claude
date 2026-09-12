"""命令行入口：

    python -m jingshui run                  # 最近一个已收盘交易日
    python -m jingshui run --date 2026-09-11 --force
    python -m jingshui run --provider free  # 零鉴权备用源
    python -m jingshui backfill --days 60   # 按时间顺序补跑最近 60 个交易日

退出码：0 成功/跳过；3 数据尚未就绪（Actions 里第二次定时任务会重试）；1 其他错误。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .client import AuthError
from .pipeline import NotReady, Runner, load_config


def _gh_output(**kv) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for k, v in kv.items():
                fh.write(f"{k}={v}\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="jingshui", description="景气 α 趋势跟随系统 · 每日选股")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="执行每日选股")
    r.add_argument("--date", help="交易日 YYYY-MM-DD（默认最近一个已收盘交易日）")
    r.add_argument("--provider", choices=["hithink", "free"])
    r.add_argument("--config", default="config.yaml")
    r.add_argument("--portfolio", default="portfolio.yaml")
    r.add_argument("--force", action="store_true", help="已有当日结果也重跑")
    r.add_argument("--allow-stale", action="store_true", help="数据未更新到目标日时按最新可用日期运行")
    r.add_argument("-v", "--verbose", action="store_true")
    bf = sub.add_parser("backfill", help="按时间顺序补跑最近 N 个交易日（已有结果的跳过）")
    bf.add_argument("--days", type=int, default=20)
    bf.add_argument("--provider", choices=["hithink", "free"])
    bf.add_argument("--config", default="config.yaml")
    bf.add_argument("--portfolio", default="portfolio.yaml")
    bf.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        runner = Runner(load_config(args.config), args.provider, args.portfolio)
        if args.cmd == "backfill":
            done = runner.backfill(args.days)
            logging.info("补跑完成：%s", [f"{r['date']}:{r['status']}" for r in done])
            _gh_output(status="ok")
            return 0
        res = runner.run(args.date, force=args.force, allow_stale=args.allow_stale)
    except NotReady as exc:
        logging.warning("%s", exc)
        _gh_output(status="not_ready")
        return 3
    except AuthError as exc:
        logging.error("认证失败：%s", exc)
        _gh_output(status="error")
        return 1
    logging.info("完成：%s", res)
    _gh_output(status=res["status"], date=res.get("date", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
