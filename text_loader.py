from __future__ import annotations

from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .http_utils import open_url


def load_text_argument(
    value: str,
    user_agent: str,
    request_timeout: float,
    default_encoding: str = "",
    use_env_proxy: bool = False,
) -> str:
    text = str(value or "").strip()
    if not text:
        return text
    if text.startswith(("http://", "https://", "file://")):
        return fetch_raw_text(
            text,
            user_agent=user_agent,
            request_timeout=request_timeout,
            default_encoding=default_encoding,
            use_env_proxy=use_env_proxy,
        )

    try:
        path = Path(text).expanduser()
    except (OSError, ValueError):
        return text
    try:
        if not path.is_file():
            return text
    except OSError:
        return text
    return path.read_text(encoding=default_encoding or "utf-8")


def fetch_raw_text(
    url: str,
    user_agent: str,
    request_timeout: float,
    default_encoding: str = "",
    use_env_proxy: bool = False,
) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": user_agent,
        },
    )
    try:
        with open_url(request, request_timeout, use_env_proxy=use_env_proxy) as response:
            body = response.read()
            encoding = response.headers.get_content_charset() or default_encoding or "utf-8"
    except HTTPError as exc:
        raise ValueError(format_remote_fetch_error(url, exc.code, str(exc.reason))) from exc
    except URLError as exc:
        raise ValueError("网络错误: {reason}".format(reason=exc.reason)) from exc
    try:
        return body.decode(encoding)
    except UnicodeDecodeError:
        return body.decode("utf-8", errors="replace")


def format_remote_fetch_error(url: str, code: int, reason: str) -> str:
    message = "HTTP {code}: {reason}".format(code=code, reason=reason)
    if code == 400 and "jsdelivr" in url:
        return (
            "{base}。提示：jsDelivr 的 GitHub 文件地址通常应为 "
            "https://cdn.jsdelivr.net/gh/<user>/<repo>@<branch>/<path/to/file> ，"
            "或 https://gcore.jsdelivr.net/gh/<user>/<repo>@<branch>/<path/to/file> "
            "；你当前这条链接看起来缺少 repo 名或分支信息"
        ).format(base=message)
    if code != 404:
        return message

    tips = []
    if "raw.githubusercontent.com" in url:
        tips.append("GitHub raw 地址通常需要包含分支名，例如 /main/ 或 /master/")
        tips.append("也请确认文件路径是否真的在该目录下")
    if "github.com" in url and "/blob/" in url:
        tips.append("你传的是 GitHub 页面链接，建议改成 raw 链接或仓库中的实际文件直链")
    if tips:
        return "{base}。提示：{tips}".format(base=message, tips="；".join(tips))
    return message
