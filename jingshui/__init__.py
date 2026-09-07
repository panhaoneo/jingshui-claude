"""景气 α 趋势跟随系统.

把《笑傲股市》(CAN SLIM) 与静水2008 问答记录蒸馏出的投资框架,
用同花顺金融数据服务 (HiThink Financial-API) 的 A 股财务与行情数据落地实现.

框架文档见 docs/03-融合投资框架.md。
本包是研究工具, 不构成投资建议。
"""

__version__ = "0.1.0"

__all__ = [
    "client",
    "indicators",
    "fundamentals",
    "market",
    "sectors",
    "screener",
    "rules",
]
