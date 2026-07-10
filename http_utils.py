from __future__ import annotations

import atexit
import gzip
import threading
import zlib
from email.message import Message
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

try:
    import httpx
except ImportError:
    httpx = None


# 重定向校验器是“每次请求”而非“每个客户端”的属性，但 httpx 客户端与 urllib opener
# 都是按线程缓存复用的。用线程本地槽位在单次 open_url 期间挂上当前请求的校验器，
# 由客户端级 event hook / 重定向 handler 在每一跳读取，避免把可变策略烤进缓存对象。
RedirectValidator = Callable[[str], None]


class ResponseTooLargeError(ValueError):
    pass


_THREAD_LOCAL = threading.local()
_ACTIVE_REDIRECT_VALIDATOR = threading.local()
_CLIENTS_LOCK = threading.Lock()
_REGISTERED_CLIENTS: list[Any] = []
_URLLIB_OPENERS: dict[bool, Any] = {}
_URLLIB_OPENERS_LOCK = threading.Lock()


def _run_active_redirect_validator(url: str) -> None:
    validator = getattr(_ACTIVE_REDIRECT_VALIDATOR, "fn", None)
    if validator is not None:
        validator(str(url))


def _validate_redirect_request(request: Any) -> None:
    # httpx 的 request event hook：自动跟随的每一跳（含首个请求）都会触发。
    _run_active_redirect_validator(str(getattr(request, "url", "")))


class _ValidatingRedirectHandler(HTTPRedirectHandler):
    # urllib 回退路径（仅在未安装 httpx 时使用）的重定向校验，逐跳放行前先校验目标。
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        _run_active_redirect_validator(str(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _BytesResponse:
    def __init__(self, body: bytes, headers: dict[str, str], url: str):
        self._body = body
        self.url = url
        message = Message()
        for key, value in headers.items():
            message[str(key)] = str(value)
        self.headers = message

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_BytesResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _open_with_urllib(
    request: Request,
    timeout: float,
    use_env_proxy: bool = False,
    max_response_bytes: int | None = None,
) -> Any:
    opener = _get_urllib_opener(use_env_proxy=use_env_proxy)
    response = opener.open(request, timeout=timeout)
    if max_response_bytes is None:
        return response
    try:
        body = _read_urllib_response_limited(response, max_response_bytes)
        return _BytesResponse(
            body,
            dict(getattr(response, "headers", {}) or {}),
            str(getattr(response, "url", request.full_url) or request.full_url),
        )
    finally:
        response.close()


def _get_urllib_opener(use_env_proxy: bool) -> Any:
    with _URLLIB_OPENERS_LOCK:
        opener = _URLLIB_OPENERS.get(use_env_proxy)
        if opener is None:
            # use_env_proxy=False 时用空 ProxyHandler 关闭代理；为 True 时不挂
            # ProxyHandler，让 urllib 沿用环境变量里的代理。两种 opener 都带上
            # 校验型重定向 handler，保证逐跳目标都过策略。
            handlers: list[Any] = [_ValidatingRedirectHandler()]
            if not use_env_proxy:
                handlers.insert(0, ProxyHandler({}))
            opener = build_opener(*handlers)
            _URLLIB_OPENERS[use_env_proxy] = opener
        return opener


def _open_with_httpx(
    request: Request,
    timeout: float,
    use_env_proxy: bool = False,
    max_response_bytes: int | None = None,
) -> _BytesResponse:
    assert httpx is not None
    client = _get_thread_local_httpx_client(use_env_proxy)
    request_kwargs = {
        "method": request.get_method(),
        "url": request.full_url,
        "headers": dict(request.header_items()),
        "content": request.data,
        "timeout": timeout,
    }
    if max_response_bytes is None:
        response = client.request(**request_kwargs)
        _raise_for_http_status(request, response)
        return _BytesResponse(
            response.content,
            dict(response.headers),
            str(response.url),
        )
    with client.stream(**request_kwargs) as response:
        _raise_for_http_status(request, response)
        _reject_oversized_content_length(response.headers, max_response_bytes)
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > max_response_bytes:
                raise ResponseTooLargeError(
                    "HTTP 响应解压后超过 {limit} 字节上限".format(
                        limit=max_response_bytes
                    )
                )
            chunks.append(chunk)
        return _BytesResponse(
            b"".join(chunks),
            dict(response.headers),
            str(response.url),
        )


def _raise_for_http_status(request: Request, response: Any) -> None:
    if int(response.status_code) < 400:
        return
    raise HTTPError(
        request.full_url,
        response.status_code,
        response.reason_phrase,
        dict(response.headers),
        None,
    )


def _reject_oversized_content_length(headers: Any, max_response_bytes: int) -> None:
    raw_length = ""
    if hasattr(headers, "get"):
        raw_length = str(headers.get("Content-Length") or "").strip()
    if not raw_length:
        return
    try:
        content_length = int(raw_length)
    except (TypeError, ValueError):
        return
    if content_length > max_response_bytes:
        raise ResponseTooLargeError(
            "HTTP Content-Length={size} 超过 {limit} 字节上限".format(
                size=content_length,
                limit=max_response_bytes,
            )
        )


def _read_urllib_response_limited(response: Any, max_response_bytes: int) -> bytes:
    headers = getattr(response, "headers", {}) or {}
    _reject_oversized_content_length(headers, max_response_bytes)
    content_encoding = ""
    if hasattr(headers, "get"):
        content_encoding = str(headers.get("Content-Encoding") or "").strip().lower()
    if content_encoding in {"", "identity"}:
        body = response.read(max_response_bytes + 1)
    elif content_encoding in {"gzip", "x-gzip"}:
        with gzip.GzipFile(fileobj=response) as decoded:
            body = decoded.read(max_response_bytes + 1)
    elif content_encoding == "deflate":
        body = _read_deflate_limited(response, max_response_bytes)
    else:
        raise ResponseTooLargeError(
            "urllib 不支持安全解压 Content-Encoding={encoding}".format(
                encoding=content_encoding
            )
        )
    if len(body) > max_response_bytes:
        raise ResponseTooLargeError(
            "HTTP 响应解压后超过 {limit} 字节上限".format(limit=max_response_bytes)
        )
    return body


def _read_deflate_limited(response: Any, max_response_bytes: int) -> bytes:
    decoder = zlib.decompressobj()
    chunks: list[bytes] = []
    total = 0
    while True:
        compressed = response.read(65536)
        if not compressed:
            break
        decoded = decoder.decompress(compressed, max_response_bytes - total + 1)
        total += len(decoded)
        if total > max_response_bytes:
            raise ResponseTooLargeError(
                "HTTP 响应解压后超过 {limit} 字节上限".format(limit=max_response_bytes)
            )
        chunks.append(decoded)
    tail = decoder.flush(max_response_bytes - total + 1)
    if total + len(tail) > max_response_bytes:
        raise ResponseTooLargeError(
            "HTTP 响应解压后超过 {limit} 字节上限".format(limit=max_response_bytes)
        )
    chunks.append(tail)
    return b"".join(chunks)


def _get_thread_local_httpx_client(use_env_proxy: bool) -> Any:
    assert httpx is not None
    client_factory = getattr(httpx, "Client", None)
    if client_factory is None:
        raise RuntimeError("httpx.Client 不可用")
    clients = getattr(_THREAD_LOCAL, "httpx_clients", None)
    if clients is None:
        clients = {}
        _THREAD_LOCAL.httpx_clients = clients
    entry = clients.get(use_env_proxy)
    if entry and entry.get("factory") is client_factory:
        return entry["client"]
    if entry:
        _safe_close(entry["client"])
    client = client_factory(
        follow_redirects=True,
        trust_env=use_env_proxy,
        event_hooks={"request": [_validate_redirect_request]},
    )
    clients[use_env_proxy] = {
        "client": client,
        "factory": client_factory,
    }
    _register_client(client)
    return client


def _register_client(client: Any) -> None:
    with _CLIENTS_LOCK:
        _REGISTERED_CLIENTS.append(client)


def _safe_close(client: Any) -> None:
    try:
        client.close()
    except Exception:
        pass


def _close_registered_clients() -> None:
    with _CLIENTS_LOCK:
        clients = list(_REGISTERED_CLIENTS)
        _REGISTERED_CLIENTS.clear()
    seen: set[int] = set()
    for client in clients:
        client_id = id(client)
        if client_id in seen:
            continue
        seen.add(client_id)
        _safe_close(client)


atexit.register(_close_registered_clients)


def open_url(
    request: Request,
    timeout: float,
    use_env_proxy: bool = False,
    redirect_validator: RedirectValidator | None = None,
    max_response_bytes: int | None = None,
) -> Any:
    # 初始目标先过一遍校验，再把校验器挂到线程本地，由后续每一跳重定向复用。
    if redirect_validator is not None:
        redirect_validator(str(request.full_url))
    previous = getattr(_ACTIVE_REDIRECT_VALIDATOR, "fn", None)
    _ACTIVE_REDIRECT_VALIDATOR.fn = redirect_validator
    try:
        if request.full_url.startswith("file://") or httpx is None:
            return _open_with_urllib(
                request,
                timeout,
                use_env_proxy=use_env_proxy,
                max_response_bytes=max_response_bytes,
            )
        try:
            return _open_with_httpx(
                request,
                timeout,
                use_env_proxy=use_env_proxy,
                max_response_bytes=max_response_bytes,
            )
        except HTTPError:
            raise
        except URLError:
            raise
        except ResponseTooLargeError:
            raise
        except Exception as exc:
            # 校验失败（含重定向到内网）属于策略拒绝，原样抛出，不要包成 URLError，
            # 以免调用方把“安全拒绝”误判成“网络偶发失败”。
            if redirect_validator is not None and isinstance(exc, ValueError):
                raise
            raise URLError(str(exc)) from exc
    finally:
        _ACTIVE_REDIRECT_VALIDATOR.fn = previous
