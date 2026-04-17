from __future__ import annotations

import atexit
import threading
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

try:
    import httpx
except ImportError:
    httpx = None


_THREAD_LOCAL = threading.local()
_CLIENTS_LOCK = threading.Lock()
_REGISTERED_CLIENTS: list[Any] = []
_URLLIB_OPENERS: dict[bool, Any] = {}
_URLLIB_OPENERS_LOCK = threading.Lock()


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
) -> Any:
    if use_env_proxy:
        return urlopen(request, timeout=timeout)
    opener = _get_urllib_opener(use_env_proxy=False)
    return opener.open(request, timeout=timeout)


def _get_urllib_opener(use_env_proxy: bool) -> Any:
    with _URLLIB_OPENERS_LOCK:
        opener = _URLLIB_OPENERS.get(use_env_proxy)
        if opener is None:
            opener = build_opener(ProxyHandler({}))
            _URLLIB_OPENERS[use_env_proxy] = opener
        return opener


def _open_with_httpx(
    request: Request,
    timeout: float,
    use_env_proxy: bool = False,
) -> _BytesResponse:
    assert httpx is not None
    client = _get_thread_local_httpx_client(use_env_proxy)
    response = client.request(
        method=request.get_method(),
        url=request.full_url,
        headers=dict(request.header_items()),
        content=request.data,
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise HTTPError(
            request.full_url,
            response.status_code,
            response.reason_phrase,
            dict(response.headers),
            None,
        )
    return _BytesResponse(
        response.content,
        dict(response.headers),
        str(response.url),
    )


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
) -> Any:
    if request.full_url.startswith("file://"):
        return _open_with_urllib(request, timeout, use_env_proxy=use_env_proxy)
    if httpx is None:
        return _open_with_urllib(request, timeout, use_env_proxy=use_env_proxy)
    try:
        return _open_with_httpx(request, timeout, use_env_proxy=use_env_proxy)
    except HTTPError:
        raise
    except Exception as exc:
        raise URLError(str(exc)) from exc
