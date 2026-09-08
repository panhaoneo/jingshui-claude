"""同花顺金融数据服务 REST 客户端.

契约来源: HiThink-Tech/Financial-API `docs/api/`。
- Base URL: https://fuyao.aicubes.cn
- 认证: HTTP Header `X-api-key`
- 成功判断: HTTP 200 且响应体 `code == 0`
- 响应信封: {code, message, request_id, data}

只依赖标准库, 不引入第三方 HTTP 库。
API Key 只从环境变量或凭据文件读取, 绝不写入代码、日志或异常信息。
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator, Mapping

LOG = logging.getLogger(__name__)

BASE_URL = "https://fuyao.aicubes.cn"
API_KEY_ENV = "HITHINK_FINANCE_API_KEY"

# 契约定义的错误码分类 (docs/api/README.md)
CALLER_FIXABLE = {1001, 1002, 1003, 1004, 2001, 2003, 3001, 3004}
RETRYABLE = {4001, 5001, 5002, 5003}
NOT_READY = 3002
RATE_LIMITED = 4001


class HithinkError(RuntimeError):
    """上游返回了非 0 的业务错误码。"""

    def __init__(self, code: int, message: str, request_id: str | None = None) -> None:
        self.code = code
        self.message = message
        self.request_id = request_id
        detail = f"code={code} message={message!r}"
        if request_id:
            detail += f" request_id={request_id}"
        super().__init__(detail)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE


class HttpStatusError(RuntimeError):
    """上游返回了非 2xx 的 HTTP 状态码, 且响应体不是业务信封。

    和 HithinkError 的区别: 那个是"服务处理了请求但业务上失败",
    这个是请求根本没进到业务层 (限流、网关、鉴权中间件)。
    两者都不是"网络不可达"。
    """

    def __init__(self, status: int, reason: str, retry_after: float | None = None, body: str = "") -> None:
        self.status = status
        self.reason = reason
        self.retry_after = retry_after
        self.body = body[:400]
        detail = f"HTTP {status} {reason}"
        if retry_after is not None:
            detail += f" (Retry-After: {retry_after:g}s)"
        if self.body:
            detail += f" body={self.body!r}"
        super().__init__(detail)

    @property
    def retryable(self) -> bool:
        # 429 限流和 5xx 网关/服务端错误值得退避重试;
        # 401/403 是鉴权或策略问题, 重试只会继续被拒。
        return self.status == 429 or 500 <= self.status < 600


class MissingApiKey(RuntimeError):
    """未配置 API Key。"""


def load_api_key(explicit: str | None = None) -> str:
    """按 显式参数 -> 环境变量 -> 用户级凭据文件 的顺序读取 API Key。

    绝不接受把 Key 写在仓库内的配置文件里。
    """
    if explicit:
        return explicit.strip()
    env = os.environ.get(API_KEY_ENV)
    if env and env.strip():
        return env.strip()
    cred = os.path.expanduser("~/.config/hithink-finance/credentials.env")
    if os.path.exists(cred):
        with open(cred, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == API_KEY_ENV and value.strip():
                    return value.strip().strip("'\"")
    raise MissingApiKey(
        f"未找到 API Key。请设置环境变量 {API_KEY_ENV}, "
        "或写入 ~/.config/hithink-finance/credentials.env。"
        "申请地址: https://fuyao.aicubes.cn/admin/"
    )


class HithinkClient:
    """同花顺金融数据服务的最小可用客户端。

    只做四件事: 拼 URL、带上 Header、检查信封、按契约分类重试。
    业务语义留给上层模块。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        opener: Any = None,
        min_interval: float = 0.0,
        max_interval: float = 8.0,
    ) -> None:
        self._api_key = load_api_key(api_key)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        # 自适应节流: 撞一次限流就把间隔翻倍, 连续成功后再慢慢收回。
        # 固定间隔挡不住突发配额 —— 第一次撞上之后就该自己降速, 而不是
        # 让后面每个请求都去撞一遍。
        self._base_interval = max(0.0, min_interval)
        self._interval = self._base_interval
        self._max_interval = max(max_interval, self._base_interval)
        self._last_sent = 0.0
        self._consecutive_ok = 0
        # opener 可注入, 便于测试时替换传输层。
        self._opener = opener or urllib.request.build_opener()

    # ---------- 节流 ----------

    @property
    def current_interval(self) -> float:
        """当前实际生效的请求间隔, 会随限流自动放大。"""
        return self._interval

    def set_min_interval(self, seconds: float) -> None:
        self._base_interval = max(0.0, seconds)
        self._interval = max(self._interval, self._base_interval)
        self._max_interval = max(self._max_interval, self._base_interval)

    def _throttle(self) -> None:
        if self._interval <= 0:
            return
        wait = self._last_sent + self._interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _note_rate_limited(self) -> None:
        self._interval = min(max(self._interval * 2.0, 0.5), self._max_interval)
        self._consecutive_ok = 0
        LOG.debug("rate limited, interval widened to %.2fs", self._interval)

    def _note_success(self) -> None:
        self._consecutive_ok += 1
        if self._consecutive_ok >= 10 and self._interval > self._base_interval:
            self._interval = max(self._base_interval, self._interval * 0.7)
            self._consecutive_ok = 0

    # ---------- 传输层 ----------

    def _raw_get(self, path: str, params: Mapping[str, Any]) -> dict:
        query = {k: v for k, v in params.items() if v is not None}
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(url, method="GET")
        req.add_header("X-api-key", self._api_key)
        req.add_header("Accept", "application/json")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # HTTPError 是 URLError 的子类, 但它意味着"服务器答复了非 2xx",
            # 不是"连不上"。必须先拦下来, 否则限流会被误报成网络不可达。
            raw = ""
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - 读不到响应体不该盖掉原始错误
                pass
            # 有些网关会在非 2xx 上仍然带回业务信封, 那就交给上层按 code 处理
            try:
                envelope = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                envelope = None
            if isinstance(envelope, dict) and "code" in envelope:
                return envelope
            raise HttpStatusError(
                exc.code, exc.reason or "", _retry_after(exc), raw
            ) from exc
        return json.loads(body)

    def get(self, path: str, **params: Any) -> Any:
        """发一次 GET, 返回信封里的 `data`。

        重试策略遵循契约: 1xxx/2xxx 是调用方错误, 不重试;
        网络错误、4001 限流和 5xxx 服务端错误在有界次数内指数退避。
        """
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self._last_sent = time.monotonic()
            try:
                envelope = self._raw_get(path, params)
            except HttpStatusError as exc:
                last_exc = exc
                if exc.status == 429:
                    self._note_rate_limited()
                if not exc.retryable or attempt >= self.max_retries:
                    raise
                self._sleep(attempt, exc.retry_after)
                continue
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise
                self._sleep(attempt)
                continue

            code = envelope.get("code")
            if code == 0:
                self._note_success()
                return envelope.get("data")

            err = HithinkError(
                int(code) if code is not None else -1,
                str(envelope.get("message", "")),
                envelope.get("request_id"),
            )
            if err.code == RATE_LIMITED:
                self._note_rate_limited()
            if err.code in CALLER_FIXABLE or err.code == NOT_READY:
                # 调用方可修复的错误和"数据尚未准备"都不该无脑重试。
                raise err
            if not err.retryable or attempt >= self.max_retries:
                raise err
            last_exc = err
            self._sleep(attempt)

        assert last_exc is not None
        raise last_exc

    def _sleep(self, attempt: int, retry_after: float | None = None) -> None:
        if retry_after is not None and retry_after > 0:
            # 上游明确说了等多久就等多久, 别用自己的退避覆盖它
            delay = min(retry_after, 60.0)
        else:
            delay = min(2.0 ** attempt, 16.0) * (0.5 + random.random() * 0.5)
        LOG.debug("retrying after %.2fs (attempt %d)", delay, attempt + 1)
        time.sleep(delay)

    # ---------- 元信息 ----------

    def search_ticker(self, q: str, limit: int = 10) -> list[dict]:
        data = self.get("/api/meta/tickers/search", q=q, limit=limit)
        return _items(data)

    def list_tickers(
        self, exchange: str = "SH,SZ", asset_type: str = "a-share", page_size: int = 1000
    ) -> Iterator[dict]:
        """分页迭代代码表。终止条件: 当前页数量 < page_size 或空页。"""
        offset = 0
        while True:
            data = self.get(
                "/api/meta/tickers/list",
                exchange=exchange,
                asset_type=asset_type,
                limit=page_size,
                offset=offset,
            )
            items = _items(data)
            yield from items
            if len(items) < page_size:
                return
            offset += page_size

    # ---------- 行情 ----------

    def price_snapshot(self, thscodes: list[str]) -> list[dict]:
        data = self.get("/api/a-share/prices/snapshot", thscodes=",".join(thscodes))
        return _items(data)

    def price_history(
        self,
        thscode: str,
        start_ms: int,
        end_ms: int,
        adjust: str = "forward",
        interval: str = "1d",
    ) -> list[dict]:
        """单只 A 股日线。区间跨度不得超过 10 年, 否则上游返回 code=1003。"""
        data = self.get(
            "/api/a-share/prices/historical",
            thscode=thscode,
            interval=interval,
            start=start_ms,
            end=end_ms,
            adjust=adjust,
        )
        return _items(data)

    # ---------- 指数与板块 ----------

    def index_catalog(self, tag: str = "industry") -> list[dict]:
        """tag 枚举: cn_concept / region / tszs / industry。"""
        data = self.get("/api/a-share-index/catalog/ths-index-list", tag=tag)
        return _items(data)

    def index_constituents(self, thscode: str) -> list[dict]:
        data = self.get(
            "/api/a-share-index/constituents/ths-stock-list", thscode=thscode
        )
        return _items(data)

    def index_history(
        self, thscode: str, start_ms: int, end_ms: int, interval: str = "1d"
    ) -> list[dict]:
        """指数没有复权概念, 不要传 adjust。"""
        data = self.get(
            "/api/a-share-index/prices/historical",
            thscode=thscode,
            interval=interval,
            start=start_ms,
            end=end_ms,
        )
        return _items(data)

    # ---------- 财务 ----------

    def income_statements(
        self, thscode: str, period: str = "quarterly", limit: int = 12
    ) -> list[dict]:
        """利润表多期序列, 按 period_end_ms 降序。limit 上限 20。"""
        data = self.get(
            "/api/a-share/financials/income-statements",
            thscode=thscode,
            period=period,
            limit=limit,
        )
        return _items(data)

    def balance_sheets(
        self, thscode: str, period: str = "annual", limit: int = 5
    ) -> list[dict]:
        data = self.get(
            "/api/a-share/financials/balance-sheets",
            thscode=thscode,
            period=period,
            limit=limit,
        )
        return _items(data)

    def cash_flow_statements(
        self, thscode: str, period: str = "annual", limit: int = 5
    ) -> list[dict]:
        data = self.get(
            "/api/a-share/financials/cash-flow-statements",
            thscode=thscode,
            period=period,
            limit=limit,
        )
        return _items(data)

    def financial_indicators(self, thscode: str, report: str) -> dict:
        """report 格式为 YYYY-[1-4], 例如 2025-1 表示一季报, 2024-4 表示年报。"""
        return self.get(
            "/api/a-share/financials/indicators", thscode=thscode, report=report
        )

    # ---------- 估值与特色数据 ----------

    def valuations(self, thscodes: list[str]) -> list[dict]:
        """单次最多 100 个原始 token (去重前计数)。"""
        if len(thscodes) > 100:
            raise ValueError("估值快照单次最多 100 个 thscode, 请自行分批")
        data = self.get(
            "/api/a-share/valuations/snapshot", thscodes=",".join(thscodes)
        )
        return _items(data)

    def dragon_tiger(self, board_type: str = "org", date: str | None = None) -> dict:
        """龙虎榜。board_type 枚举: all / org / hot_money。date 格式 YYYY-MM-DD。"""
        return self.get(
            "/api/a-share/special-data/dragon-tiger-list",
            board_type=board_type,
            date=date,
        )


def _retry_after(exc: "urllib.error.HTTPError") -> float | None:
    """解析 Retry-After 响应头。只支持秒数形式, HTTP 日期形式返回 None。"""
    try:
        raw = exc.headers.get("Retry-After") if exc.headers else None
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return float(str(raw).strip())
    except ValueError:
        return None


def _items(data: Any) -> list[dict]:
    """从 `data` 里取出列表载荷。

    契约里列表载荷统一放在 `item` 键下; 业务错误时 data 为 null,
    此时不得把 null 当成"成功但空"。
    """
    if data is None:
        raise HithinkError(-1, "data 为 null, 不能当作空结果处理")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        items = data.get("item")
        if items is None:
            return []
        return list(items)
    raise TypeError(f"无法从 {type(data).__name__} 中提取列表载荷")
