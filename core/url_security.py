from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit


class UnsafeUrlError(ValueError):
    """Raised when a URL is not safe for user-triggered fetching."""


@dataclass(frozen=True)
class UrlSafetyPolicy:
    allow_unsafe_urls: bool = False
    resolve_hostnames: bool = True


def validate_user_fetch_url(
    url: str,
    *,
    policy: UrlSafetyPolicy | None = None,
    label: str = "URL",
) -> str:
    normalized = str(url or "").strip()
    if not normalized:
        raise UnsafeUrlError("{label} 不能为空".format(label=label))
    active_policy = policy or UrlSafetyPolicy()
    if active_policy.allow_unsafe_urls:
        return normalized

    parsed = urlsplit(normalized)
    scheme = str(parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise UnsafeUrlError(
            "{label} 仅允许 http/https，当前 scheme={scheme}".format(
                label=label,
                scheme=scheme or "(empty)",
            )
        )

    host = str(parsed.hostname or "").strip()
    if not host:
        raise UnsafeUrlError("{label} 缺少主机名".format(label=label))
    if _is_blocked_hostname(host):
        raise UnsafeUrlError(
            "{label} 指向本机或内网地址，已拒绝: {host}".format(
                label=label,
                host=host,
            )
        )
    if active_policy.resolve_hostnames:
        for address in _resolve_host_addresses(host):
            if _is_blocked_ip(address):
                raise UnsafeUrlError(
                    "{label} 解析到本机或内网地址，已拒绝: {host} -> {address}".format(
                        label=label,
                        host=host,
                        address=address,
                    )
                )
    return normalized


def build_redirect_validator(
    policy: UrlSafetyPolicy | None = None,
    *,
    label: str = "请求 URL",
) -> Callable[[str], None] | None:
    """构造逐跳 URL 校验器，供 http 客户端在每次请求/重定向时调用。

    策略放行不安全 URL 时返回 None，让调用方完全跳过校验开销。
    """

    active_policy = policy or UrlSafetyPolicy()
    if active_policy.allow_unsafe_urls:
        return None

    def _validate(url: str) -> None:
        validate_user_fetch_url(url, policy=active_policy, label=label)

    return _validate


def _is_blocked_hostname(host: str) -> bool:
    lowered = host.strip().rstrip(".").lower()
    if lowered in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return _is_blocked_ip(ipaddress.ip_address(_strip_ipv6_brackets(lowered)))
    except ValueError:
        return False


def _resolve_host_addresses(host: str) -> list[str]:
    try:
        entries = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError(
            "无法解析 URL 主机名，已拒绝: {host}".format(host=host)
        ) from exc
    addresses: list[str] = []
    for entry in entries:
        sockaddr = entry[4]
        if not sockaddr:
            continue
        address = str(sockaddr[0] or "").strip()
        if address and address not in addresses:
            addresses.append(address)
    return addresses


def _is_blocked_ip(address: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    ip = (
        address
        if isinstance(address, (ipaddress.IPv4Address, ipaddress.IPv6Address))
        else ipaddress.ip_address(address)
    )
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _strip_ipv6_brackets(host: str) -> str:
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host
