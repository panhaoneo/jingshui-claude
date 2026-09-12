from __future__ import annotations


def make_provider(name: str, cfg: dict, cache):
    if name == "hithink":
        from .hithink import HiThinkProvider
        return HiThinkProvider(cfg, cache)
    if name == "free":
        from .free import FreeProvider
        return FreeProvider(cfg, cache)
    raise ValueError(f"unknown provider: {name}")
