"""数据源接口。两个实现：hithink（同花顺，主源）与 free（公开接口，备用）。

财报统一为「累计口径」记录（A 股定期报告原样），单季化在 fundamentals.py 里做：
    {period_end, report_date, fiscal_year, quarter(1-4), revenue, parent_np, eps}
资产负债表（年度）：{period_end, report_date, fiscal_year, equity}
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from ..models import PricePanel


class Provider(ABC):
    name = "base"
    supports_org_flow = False  # 是否提供龙虎榜机构净额（I 因子代理）

    @abstractmethod
    def trading_days(self) -> list[pd.Timestamp]: ...

    @abstractmethod
    def universe(self) -> pd.DataFrame:
        """[thscode, name]，全部 A 股。"""

    @abstractmethod
    def price_panel(self, asof: pd.Timestamp, keep_days: int) -> PricePanel: ...

    @abstractmethod
    def index_bars(self, code: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """[date, open, high, low, close, volume, turnover]"""

    @abstractmethod
    def sector_list(self) -> pd.DataFrame:
        """[code, name]"""

    @abstractmethod
    def sector_constituents(self, code: str) -> pd.DataFrame:
        """[thscode, name]"""

    @abstractmethod
    def financials(self, thscode: str) -> dict:
        """{"quarterly": [...], "annual": [...], "balance": [...]}"""

    def lhb_org(self, date: pd.Timestamp) -> pd.DataFrame | None:
        """[thscode, org_net_value]；不支持返回 None。"""
        return None

    def valuations(self, codes: list[str]) -> pd.DataFrame:
        """[thscode, pe_ttm, pb_mrq]，仅作事后记录，不参与筛选。"""
        return pd.DataFrame(columns=["thscode", "pe_ttm", "pb_mrq"])

    def float_mcap(self, codes: list[str]) -> dict[str, float]:
        """流通市值（亿元），尽力而为。"""
        from .free import tencent_float_mcap
        try:
            return tencent_float_mcap(codes)
        except Exception:
            return {}
