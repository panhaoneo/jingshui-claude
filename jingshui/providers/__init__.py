"""数据提供方。

两个实现暴露**完全相同的方法签名**, 因此 pipeline 无需知道自己在用哪一个:

- ``hithink``: 同花顺金融数据服务 (需 API Key, 有配额)
- ``free``:    腾讯 + 新浪 + 东财的公开接口 (零鉴权, 无配额)

两者的能力差异见 FreeProvider 的类文档, 不要假设它们等价。
"""

from __future__ import annotations

from typing import Any

PROVIDERS = ("hithink", "free")


def build_provider(name: str = "hithink", **kwargs: Any):
    """按名字构造数据提供方。"""
    if name == "hithink":
        from ..client import HithinkClient

        return HithinkClient(**kwargs)
    if name == "free":
        from .free import FreeProvider

        return FreeProvider(**kwargs)
    raise ValueError(f"未知的数据源 {name!r}, 可选: {', '.join(PROVIDERS)}")
