from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request

from ..http_utils import open_url


@dataclass(frozen=True)
class SessionScraperConfig:
    user_agent: str
    use_env_proxy: bool = False
    max_retries: int = 2
    retry_backoff: float = 1.5
    per_host_limit: int = 2


@dataclass(frozen=True)
class ScraperResponse:
    body: bytes
    url: str
    headers: Any


class SessionScraper:
    _HOST_LIMITERS_LOCK = threading.Lock()
    _HOST_LIMITERS: dict[tuple[str, int], threading.BoundedSemaphore] = {}

    def __init__(self, config: SessionScraperConfig):
        self.config = config

    def request(
        self,
        url: str,
        headers: Mapping[str, Any] | None = None,
        method: str = "GET",
        body: bytes | None = None,
        timeout: float = 20.0,
    ) -> ScraperResponse:
        normalized_url = str(url or "").strip()
        if not normalized_url:
            raise ValueError("请求 URL 不能为空")

        request_headers = self._build_headers(normalized_url, headers, body)
        request = Request(
            normalized_url,
            headers=request_headers,
            data=body,
            method=(method or "GET").upper(),
        )
        limiter = self._get_host_limiter(normalized_url)
        last_error: Exception | None = None
        attempts = max(0, int(self.config.max_retries))
        for attempt in range(attempts + 1):
            with limiter:
                try:
                    with open_url(
                        request,
                        timeout,
                        use_env_proxy=self.config.use_env_proxy,
                    ) as response:
                        return ScraperResponse(
                            body=response.read(),
                            url=str(getattr(response, "url", normalized_url) or normalized_url),
                            headers=getattr(response, "headers", {}),
                        )
                except HTTPError as exc:
                    last_error = exc
                    if not self._should_retry_http(exc, attempt, attempts):
                        raise
                except URLError as exc:
                    last_error = exc
                    if not self._should_retry_network(exc, attempt, attempts):
                        raise
            self._sleep_before_retry(attempt)

        if last_error is not None:
            raise last_error
        raise RuntimeError("请求失败，但未捕获具体错误")

    def _build_headers(
        self,
        url: str,
        headers: Mapping[str, Any] | None,
        body: bytes | None,
    ) -> dict[str, str]:
        request_headers: dict[str, str] = {}
        for key, value in dict(headers or {}).items():
            if value is None:
                continue
            request_headers[str(key)] = str(value)

        request_headers.setdefault("User-Agent", self.config.user_agent)
        origin = self._extract_origin(url)
        referer = request_headers.get("Referer") or origin
        if referer:
            request_headers.setdefault("Referer", referer)
        derived_origin = self._extract_origin(request_headers.get("Referer") or url)
        if derived_origin:
            request_headers.setdefault("Origin", derived_origin)
        if body is not None and "Content-Type" not in request_headers:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        return request_headers

    def _get_host_limiter(self, url: str) -> threading.BoundedSemaphore:
        parsed = urlsplit(url)
        host = str(parsed.netloc or "")
        limit = max(1, int(self.config.per_host_limit))
        key = (host, limit)
        with self._HOST_LIMITERS_LOCK:
            limiter = self._HOST_LIMITERS.get(key)
            if limiter is None:
                limiter = threading.BoundedSemaphore(limit)
                self._HOST_LIMITERS[key] = limiter
            return limiter

    def _extract_origin(self, url: str) -> str:
        parsed = urlsplit(str(url or "").strip())
        if not parsed.scheme or not parsed.netloc:
            return ""
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")

    def _should_retry_http(self, exc: HTTPError, attempt: int, attempts: int) -> bool:
        if attempt >= attempts:
            return False
        return int(getattr(exc, "code", 0) or 0) in (408, 425, 429, 500, 502, 503, 504)

    def _should_retry_network(self, exc: URLError, attempt: int, attempts: int) -> bool:
        if attempt >= attempts:
            return False
        reason = str(getattr(exc, "reason", "") or "").lower()
        if not reason:
            return True
        return any(
            marker in reason
            for marker in (
                "timeout",
                "timed out",
                "tempor",
                "reset",
                "refused",
                "unreach",
                "remote end closed",
            )
        )

    def _sleep_before_retry(self, attempt: int) -> None:
        delay = max(0.0, float(self.config.retry_backoff or 0.0)) ** max(0, attempt)
        if delay <= 0:
            return
        time.sleep(min(delay, 5.0))
