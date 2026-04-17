from __future__ import annotations

from typing import Any
from urllib.request import ProxyHandler, Request, build_opener, urlopen


def open_url(
    request: Request,
    timeout: float,
    use_env_proxy: bool = False,
) -> Any:
    if use_env_proxy:
        return urlopen(request, timeout=timeout)
    opener = build_opener(ProxyHandler({}))
    return opener.open(request, timeout=timeout)
