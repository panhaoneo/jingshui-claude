"""本地缓存：大结果一律落盘（docs/04 §3.3）。GitHub Actions 用 actions/cache 在两次运行间保留。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import pandas as pd


class Cache:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def age_days(p: Path) -> float:
        return (time.time() - p.stat().st_mtime) / 86400 if p.exists() else float("inf")

    def json(self, key: str, ttl_days: float | None, fetch: Callable[[], Any]) -> Any:
        p = self.path(f"{key}.json")
        if p.exists() and (ttl_days is None or self.age_days(p) < ttl_days):
            return json.loads(p.read_text(encoding="utf-8"))
        data = fetch()
        self.write_json(p, data)
        return data

    @staticmethod
    def write_json(p: Path, data: Any) -> None:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(p)

    def frame(self, key: str, ttl_days: float | None, fetch: Callable[[], pd.DataFrame]) -> pd.DataFrame:
        p = self.path(f"{key}.parquet")
        if p.exists() and (ttl_days is None or self.age_days(p) < ttl_days):
            return pd.read_parquet(p)
        df = fetch()
        df.to_parquet(p, index=False)
        return df
