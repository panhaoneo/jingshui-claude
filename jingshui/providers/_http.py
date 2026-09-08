"""公开数据源共用的 HTTP 取数helper。只用标准库。

节流是这里的重点: 东财对住宅 IP 有连接级风控, 社区实测并发不限流可以吃到
20 小时以上的 IP 级封禁。所以按域名分别限流, 东财默认间隔 1.2 秒且串行。
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

LOG = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 按域名的最小请求间隔 (秒)。东财的值来自该数据源项目记录的防封铁律:
# 串行、间隔 >= 1 秒、带正常 UA。腾讯和新浪不封 IP, 只在高频下限流。
DOMAIN_MIN_INTERVAL = {
    "eastmoney.com": 1.2,
    "gtimg.cn": 0.15,
    "sina.cn": 0.3,
    "sina.com.cn": 0.3,
    "10jqka.com.cn": 0.3,
}
DEFAULT_MIN_INTERVAL = 0.3


class FetchError(RuntimeError):
    """取数失败。"""


class Throttle:
    """按域名的串行节流器, 撞到限流会自动放大间隔。"""

    def __init__(self, scale: float = 1.0, max_interval: float = 10.0) -> None:
        self.scale = max(0.0, scale)
        self.max_interval = max_interval
        self._last: dict[str, float] = {}
        self._extra: dict[str, float] = {}

    @staticmethod
    def domain_of(url: str) -> str:
        host = urllib.parse.urlparse(url).hostname or ""
        parts = host.split(".")
        # 取二级域名, 让 push2/push2his/datacenter-web 共用同一个限流面
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    def interval_for(self, domain: str) -> float:
        base = DOMAIN_MIN_INTERVAL.get(domain, DEFAULT_MIN_INTERVAL) * self.scale
        return min(base + self._extra.get(domain, 0.0), self.max_interval)

    def wait(self, url: str) -> None:
        domain = self.domain_of(url)
        interval = self.interval_for(domain)
        if interval <= 0:
            return
        last = self._last.get(domain)
        if last is not None:
            remaining = last + interval - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        self._last[domain] = time.monotonic()

    def penalise(self, url: str) -> None:
        domain = self.domain_of(url)
        self._extra[domain] = min(self._extra.get(domain, 0.0) * 2 or 1.0, self.max_interval)
        LOG.warning("%s 触发限流, 间隔放大到 %.1fs", domain, self.interval_for(domain))

    def relax(self, url: str) -> None:
        domain = self.domain_of(url)
        if self._extra.get(domain):
            self._extra[domain] *= 0.8
            if self._extra[domain] < 0.05:
                self._extra.pop(domain, None)


def fetch(
    url: str,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    throttle: Throttle | None = None,
    timeout: float = 15.0,
    encoding: str = "utf-8",
    max_retries: int = 2,
    opener: Any = None,
) -> str:
    """GET 一个 URL, 返回文本。带节流与有界重试。"""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    open_fn = (opener or urllib.request).urlopen if opener is None else opener.open

    last: Exception | None = None
    for attempt in range(max_retries + 1):
        if throttle:
            throttle.wait(url)
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with open_fn(req, timeout=timeout) as resp:
                body = resp.read().decode(encoding, errors="replace")
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (403, 429) and throttle:
                throttle.penalise(url)
            if exc.code not in (403, 429, 500, 502, 503, 504) or attempt >= max_retries:
                raise FetchError(f"{url.split('?')[0]} 返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if attempt >= max_retries:
                raise FetchError(f"{url.split('?')[0]} 请求失败: {exc}") from exc
        else:
            if throttle:
                throttle.relax(url)
            return body
        time.sleep(min(2.0 ** attempt, 8.0) * (0.5 + random.random() * 0.5))

    raise FetchError(f"{url.split('?')[0]} 重试耗尽: {last}")


def fetch_json(url: str, **kwargs: Any) -> Any:
    text = fetch(url, **kwargs)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FetchError(f"{url.split('?')[0]} 返回的不是合法 JSON: {text[:200]!r}") from exc
