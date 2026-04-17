from __future__ import annotations

from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

try:
    import httpx
except ImportError:
    httpx = None


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
    opener = build_opener(ProxyHandler({}))
    return opener.open(request, timeout=timeout)


def _open_with_httpx(
    request: Request,
    timeout: float,
    use_env_proxy: bool = False,
) -> _BytesResponse:
    assert httpx is not None
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        trust_env=use_env_proxy,
    ) as client:
        response = client.request(
            method=request.get_method(),
            url=request.full_url,
            headers=dict(request.header_items()),
            content=request.data,
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
