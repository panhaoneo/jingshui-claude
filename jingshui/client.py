"""同花顺金融数据服务（HiThink Financial-API）REST 客户端。

调用纪律（docs/04-数据接口映射.md §3）：
- 成功 = HTTP 200 且 code == 0；
- 1xxx/2xxx/3001/3004 调用方可修复，不重试；3002 数据未就绪，不重试、不补零；
- 4001 限流、5xxx 服务端异常、网络错误：指数退避，最多 3 次；
- Key 只走 Header，绝不进入 URL、日志或异常信息。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import requests

BASE_URL = "https://fuyao.aicubes.cn"
ENV_KEY = "HITHINK_FINANCE_API_KEY"
CREDENTIALS_FILE = Path.home() / ".config" / "hithink-finance" / "credentials.env"

NON_RETRYABLE = {1001, 1002, 1003, 1004, 2001, 2002, 2003, 2004, 3001, 3004}
NOT_READY = {3002, 4040}
RETRYABLE = {4001, 5001, 5002, 5003}

log = logging.getLogger(__name__)


class ApiError(RuntimeError):
    def __init__(self, code: int | None, message: str, request_id: str | None = None, path: str = ""):
        self.code = code
        self.request_id = request_id
        self.path = path
        super().__init__(f"{path} code={code} message={message} request_id={request_id}")


class DataNotReady(ApiError):
    """code 3002/4040：上游数据尚未准备好。调用方应稍后再查，不得补零。"""


class AuthError(ApiError):
    """code 2xxx：Key 缺失、无效或无权限。"""


def load_api_key() -> str | None:
    key = os.environ.get(ENV_KEY, "").strip()
    if key:
        return key
    if CREDENTIALS_FILE.exists():
        for line in CREDENTIALS_FILE.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == ENV_KEY and value.strip():
                return value.strip().strip('"').strip("'")
    return None


class HiThinkClient:
    def __init__(self, api_key: str | None = None, base_url: str = BASE_URL,
                 min_interval: float = 0.12, max_retries: int = 3, timeout: float = 30.0,
                 session: requests.Session | None = None):
        self._key = api_key or load_api_key()
        if not self._key:
            raise AuthError(None, f"未找到 API Key：请设置环境变量 {ENV_KEY} 或 {CREDENTIALS_FILE}")
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"X-api-key": self._key, "User-Agent": "jingshui-xuangu/1.0"})
        self._lock = threading.Lock()
        self._last = 0.0
        self.calls = 0

    def __repr__(self) -> str:  # 不暴露 Key
        return f"HiThinkClient(base_url={self.base_url!r})"

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """返回响应信封里的 data；失败抛 ApiError 子类。"""
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(min(2 ** attempt, 20))
            self._throttle()
            self.calls += 1
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:  # 网络错误：可重试
                last_exc = ApiError(None, f"network error: {type(exc).__name__}", path=path)
                continue
            if resp.status_code >= 500 or resp.status_code == 429:
                last_exc = ApiError(resp.status_code, f"HTTP {resp.status_code}", path=path)
                continue
            try:
                body = resp.json()
            except ValueError:
                last_exc = ApiError(resp.status_code, "non-JSON response", path=path)
                continue
            code = body.get("code")
            rid = body.get("request_id")
            msg = str(body.get("message", ""))
            if resp.status_code == 200 and code == 0:
                return body.get("data")
            if code in RETRYABLE:
                last_exc = ApiError(code, msg, rid, path)
                continue
            if code in NOT_READY:
                raise DataNotReady(code, msg, rid, path)
            if code in (2001, 2002, 2003, 2004) or resp.status_code in (401, 403):
                raise AuthError(code, msg, rid, path)
            raise ApiError(code, msg, rid, path)
        assert last_exc is not None
        raise last_exc

    def download(self, sign: Callable[[], str], dest: Path, chunk: int = 1 << 20) -> Path:
        """下载预签名 URL（S3，不携带 Key）。预签名只有约 5 分钟有效期：
        每次重试都重新签名，并用 Range 从断点续传。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.unlink(missing_ok=True)
        for attempt in range(self.max_retries + 2):
            done = tmp.stat().st_size if tmp.exists() else 0
            headers = {"Range": f"bytes={done}-"} if done else {}
            try:
                with requests.get(sign(), headers=headers, stream=True, timeout=(15, 60)) as r:
                    if r.status_code == 416:  # 已下载完整
                        break
                    r.raise_for_status()
                    mode = "ab" if done and r.status_code == 206 else "wb"
                    with open(tmp, mode) as fh:
                        for block in r.iter_content(chunk):
                            fh.write(block)
                break
            except requests.RequestException as exc:
                log.warning("下载中断（%s），第 %d 次，已下载 %.1f MB，续传", type(exc).__name__,
                            attempt + 1, (tmp.stat().st_size if tmp.exists() else 0) / 1e6)
                time.sleep(2 ** attempt)
        else:
            raise ApiError(None, "download failed", path="presigned-url")
        tmp.replace(dest)
        return dest
