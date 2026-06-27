from __future__ import annotations

import ast
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List


REGISTRY_SCHEMA_VERSION = 1
JS_RULE_MARKERS = ("<js>", "@js:")
UNSUPPORTED_JS_TOKENS = (
    "java.ajax",
    "java.post",
    "startbrowserawait",
    "fetch(",
    "xmlhttprequest",
    "document.",
    "window.",
    "location.",
    "getverificationcode",
    "getcookie",
    "cookie.",
    "source.getlogininfomap",
)
WEBVIEW_MARKERS = (
    '"webview":true',
    '"webview": true',
    "'webview':true",
    "'webview': true",
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def slugify(value: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", _clean_text(value))
    slug = slug.strip("-").lower()
    return slug or "source"


def make_source_id(name: str, source_url: str) -> str:
    digest = hashlib.sha1(
        json.dumps(
            {
                "name": _clean_text(name),
                "source_url": _clean_text(source_url),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:12]
    return "{slug}-{digest}".format(slug=slugify(name)[:32], digest=digest)


def _stringify_dict(data: Dict[str, Any]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            result[str(key)] = json.dumps(value, ensure_ascii=False)
        else:
            result[str(key)] = _clean_text(value)
    return result


def parse_headers(raw_headers: Any) -> Dict[str, str]:
    if isinstance(raw_headers, dict):
        return _stringify_dict(raw_headers)

    text = _clean_text(raw_headers)
    if not text:
        return {}

    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None

    if not isinstance(parsed, dict):
        try:
            literal = ast.literal_eval(text)
        except Exception:
            literal = None
        if isinstance(literal, dict):
            parsed = literal

    if isinstance(parsed, dict):
        return _stringify_dict(parsed)

    headers: Dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[_clean_text(key)] = _clean_text(value)
    return headers


def normalize_rule_block(value: Any) -> Dict[str, str]:
    if isinstance(value, dict):
        return _stringify_dict(value)
    text = _clean_text(value)
    if not text:
        return {}
    return {"__default__": text}


def _iter_string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_string_values(item)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_string_values(item)


def _contains_js_marker(value: Any) -> bool:
    for item in _iter_string_values(value):
        lowered = item.lower()
        if any(marker in lowered for marker in JS_RULE_MARKERS):
            return True
    return False


def _contains_unsupported_js(value: Any) -> bool:
    for item in _iter_string_values(value):
        lowered = str(item or "").lower()
        if any(token in lowered for token in UNSUPPORTED_JS_TOKENS):
            return True
        if re.search(
            r"\bjava\.get\s*\(\s*(baseurl|sourceurl|book\.|source\.|url\b|['\"]https?://)",
            lowered,
        ):
            return True
    return False


def _contains_webview_marker(value: Any) -> bool:
    for item in _iter_string_values(value):
        lowered = str(item or "").lower()
        if any(marker in lowered for marker in WEBVIEW_MARKERS):
            return True
        if "webview" in lowered and "true" in lowered:
            return True
    return False


def _looks_like_script_or_login_flow(value: Any) -> bool:
    text = _clean_text(value).lower()
    if not text:
        return False
    if text.startswith(("http://", "https://")):
        return True
    if any(marker in text for marker in JS_RULE_MARKERS):
        return True
    return any(marker in text for marker in ("function", "=>", "java.", "eval("))


def normalize_book_source(raw_source: Dict[str, Any]) -> Dict[str, Any]:
    source_name = _clean_text(
        raw_source.get("bookSourceName")
        or raw_source.get("sourceName")
        or raw_source.get("name")
        or raw_source.get("title")
        or "未命名书源"
    )
    source_url = _clean_text(
        raw_source.get("bookSourceUrl")
        or raw_source.get("sourceUrl")
        or raw_source.get("url")
    )
    source_id = make_source_id(source_name, source_url)
    enabled = bool(raw_source.get("enabled", True))

    normalized = {
        "source_id": source_id,
        "name": source_name,
        "source_url": source_url,
        "group": _clean_text(
            raw_source.get("bookSourceGroup")
            or raw_source.get("sourceGroup")
            or raw_source.get("group")
        ),
        "enabled": enabled,
        "search_url": _clean_text(raw_source.get("searchUrl")),
        "explore_url": _clean_text(raw_source.get("exploreUrl")),
        "clean_rule_url": _clean_text(
            raw_source.get("cleanRuleUrl")
            or raw_source.get("ruleCleanUrl")
            or raw_source.get("defaultRuleUrl")
            or raw_source.get("cleanUrl")
        ),
        "book_source_type": raw_source.get("bookSourceType", 0),
        "headers": parse_headers(raw_source.get("header")),
        "rule_search": normalize_rule_block(raw_source.get("ruleSearch")),
        "rule_book_info": normalize_rule_block(raw_source.get("ruleBookInfo")),
        "rule_toc": normalize_rule_block(raw_source.get("ruleToc")),
        "rule_content": normalize_rule_block(raw_source.get("ruleContent")),
        "rule_explore": normalize_rule_block(raw_source.get("ruleExplore")),
        "respond_time": raw_source.get("respondTime", 0),
        "weight": raw_source.get("weight", 0),
        "login_url": _clean_text(raw_source.get("loginUrl")),
        "single_url": bool(raw_source.get("singleUrl", False)),
        "load_with_base_url": bool(raw_source.get("loadWithBaseUrl", False)),
        "enable_js": bool(raw_source.get("enableJs", False)),
        "js_lib": _clean_text(raw_source.get("jsLib")),
        "has_js_lib": bool(_clean_text(raw_source.get("jsLib"))),
        "has_web_js": bool(_clean_text(raw_source.get("webJs"))),
        "has_login_flow": bool(raw_source.get("loginUi"))
        or _looks_like_script_or_login_flow(raw_source.get("loginUrl")),
        "search_url_uses_js": _contains_js_marker(raw_source.get("searchUrl")),
        "rule_search_uses_js": _contains_js_marker(raw_source.get("ruleSearch")),
        "rule_book_info_uses_js": _contains_js_marker(raw_source.get("ruleBookInfo")),
        "rule_toc_uses_js": _contains_js_marker(raw_source.get("ruleToc")),
        "rule_content_uses_js": _contains_js_marker(raw_source.get("ruleContent")),
        "js_lib_uses_unsupported_js": _contains_unsupported_js(
            raw_source.get("jsLib")
        ),
        "search_url_uses_unsupported_js": _contains_unsupported_js(
            raw_source.get("searchUrl")
        ),
        "rule_search_uses_unsupported_js": _contains_unsupported_js(
            raw_source.get("ruleSearch")
        ),
        "rule_book_info_uses_unsupported_js": _contains_unsupported_js(
            raw_source.get("ruleBookInfo")
        ),
        "rule_toc_uses_unsupported_js": _contains_unsupported_js(
            raw_source.get("ruleToc")
        ),
        "rule_content_uses_unsupported_js": _contains_unsupported_js(
            raw_source.get("ruleContent")
        ),
        "search_uses_webview": _contains_webview_marker(
            (
                raw_source.get("searchUrl"),
                raw_source.get("ruleSearch"),
                raw_source.get("header"),
            )
        ),
        "rule_book_info_uses_webview": _contains_webview_marker(
            raw_source.get("ruleBookInfo")
        ),
        "rule_toc_uses_webview": _contains_webview_marker(raw_source.get("ruleToc")),
        "rule_content_uses_webview": _contains_webview_marker(
            raw_source.get("ruleContent")
        ),
        "last_imported_at": time.time(),
    }
    return normalized


@dataclass
class SourceSummary:
    source_id: str
    name: str
    source_url: str
    enabled: bool
    group: str
    search_url: str
    clean_rule_url: str
    book_source_type: int
    single_url: bool
    enable_js: bool
    has_js_lib: bool
    has_web_js: bool
    has_login_flow: bool
    has_rule_search: bool
    has_rule_book_info: bool
    has_rule_toc: bool
    has_rule_content: bool
    search_uses_js: bool
    download_uses_js: bool
    search_uses_unsupported_js: bool
    download_uses_unsupported_js: bool
    search_uses_webview: bool
    download_uses_webview: bool
    supports_search: bool
    supports_download: bool
    issues: List[str]
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_source_summary(
    normalized: Dict[str, Any], updated_at: float
) -> SourceSummary:
    has_required_search = bool(normalized.get("search_url")) and bool(
        normalized.get("rule_search")
    )
    has_required_download = (
        bool(normalized.get("rule_book_info"))
        and bool(normalized.get("rule_toc"))
        and bool(normalized.get("rule_content"))
    )
    search_uses_js = bool(normalized.get("enable_js")) or any(
        (
            normalized.get("search_url_uses_js"),
            normalized.get("rule_search_uses_js"),
        )
    )
    download_uses_js = bool(normalized.get("enable_js")) or any(
        (
            normalized.get("rule_book_info_uses_js"),
            normalized.get("rule_toc_uses_js"),
            normalized.get("rule_content_uses_js"),
        )
    )
    search_uses_unsupported_js = any(
        (
            normalized.get("search_url_uses_unsupported_js"),
            normalized.get("rule_search_uses_unsupported_js"),
        )
    )
    download_uses_unsupported_js = any(
        (
            normalized.get("js_lib_uses_unsupported_js"),
            normalized.get("rule_book_info_uses_unsupported_js"),
            normalized.get("rule_toc_uses_unsupported_js"),
            normalized.get("rule_content_uses_unsupported_js"),
        )
    )
    search_uses_webview = bool(normalized.get("search_uses_webview"))
    download_uses_webview = bool(
        normalized.get("rule_book_info_uses_webview")
        or normalized.get("rule_toc_uses_webview")
        or normalized.get("rule_content_uses_webview")
        or normalized.get("has_web_js")
    )
    supports_search = (
        has_required_search
        and not search_uses_unsupported_js
        and not search_uses_webview
        and not bool(normalized.get("has_login_flow"))
    )
    supports_download = (
        has_required_download
        and not download_uses_unsupported_js
        and not download_uses_webview
        and not bool(normalized.get("has_login_flow"))
    )
    issues: List[str] = []
    if normalized.get("single_url") and not has_required_search:
        issues.append("检测到 singleUrl 单链接/RSS 源，当前不支持按书名搜索下载")
    if search_uses_unsupported_js:
        issues.append(
            "searchUrl/ruleSearch 含网络、浏览器或 App 环境 JS，当前无法按书名搜索"
        )
    if search_uses_webview:
        issues.append("搜索规则依赖 webView/浏览器渲染，当前服务端模式不支持")
    download_unsupported_blocks = []
    if normalized.get("js_lib_uses_unsupported_js"):
        download_unsupported_blocks.append("jsLib")
    if normalized.get("rule_book_info_uses_unsupported_js"):
        download_unsupported_blocks.append("ruleBookInfo")
    if normalized.get("rule_toc_uses_unsupported_js"):
        download_unsupported_blocks.append("ruleToc")
    if normalized.get("rule_content_uses_unsupported_js"):
        download_unsupported_blocks.append("ruleContent")
    if download_unsupported_blocks:
        issues.append(
            "{blocks} 含网络、浏览器或 App 环境 JS，当前无法稳定抓目录/正文并下载 TXT".format(
                blocks="/".join(download_unsupported_blocks)
            )
        )
    download_webview_blocks = []
    if normalized.get("rule_book_info_uses_webview"):
        download_webview_blocks.append("ruleBookInfo")
    if normalized.get("rule_toc_uses_webview"):
        download_webview_blocks.append("ruleToc")
    if normalized.get("rule_content_uses_webview"):
        download_webview_blocks.append("ruleContent")
    if normalized.get("has_web_js"):
        issues.append("检测到 webJs/browser 脚本，当前服务端模式不支持 webView")
    if download_webview_blocks:
        issues.append(
            "{blocks} 依赖 webView/浏览器渲染，当前服务端模式不支持 TXT 下载".format(
                blocks="/".join(download_webview_blocks)
            )
        )
    if normalized.get("has_login_flow"):
        issues.append(
            "检测到 loginUrl/loginUi 登录或脚本流程；当前轻量 JS 路线不支持登录态与脚本登录"
        )
    if not has_required_search:
        issues.append("缺少 searchUrl 或 ruleSearch，无法按书名搜索")
    if not has_required_download:
        issues.append("缺少 ruleBookInfo/ruleToc/ruleContent，无法自动抓目录并下载 TXT")
    return SourceSummary(
        source_id=normalized["source_id"],
        name=normalized["name"],
        source_url=normalized["source_url"],
        enabled=bool(normalized["enabled"]),
        group=normalized.get("group", ""),
        search_url=normalized.get("search_url", ""),
        clean_rule_url=normalized.get("clean_rule_url", ""),
        book_source_type=int(normalized.get("book_source_type", 0) or 0),
        single_url=bool(normalized.get("single_url", False)),
        enable_js=bool(normalized.get("enable_js", False)),
        has_js_lib=bool(normalized.get("has_js_lib", False)),
        has_web_js=bool(normalized.get("has_web_js", False)),
        has_login_flow=bool(normalized.get("has_login_flow", False)),
        has_rule_search=bool(normalized.get("rule_search")),
        has_rule_book_info=bool(normalized.get("rule_book_info")),
        has_rule_toc=bool(normalized.get("rule_toc")),
        has_rule_content=bool(normalized.get("rule_content")),
        search_uses_js=bool(search_uses_js),
        download_uses_js=bool(download_uses_js),
        search_uses_unsupported_js=bool(search_uses_unsupported_js),
        download_uses_unsupported_js=bool(download_uses_unsupported_js),
        search_uses_webview=bool(search_uses_webview),
        download_uses_webview=bool(download_uses_webview),
        supports_search=supports_search,
        supports_download=supports_download,
        issues=issues,
        updated_at=updated_at,
    )


def parse_source_payload(raw_text: str) -> List[Dict[str, Any]]:
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError("书源 JSON 解析失败: {error}".format(error=exc)) from exc

    if isinstance(parsed, dict):
        if isinstance(parsed.get("sources"), list):
            payload = parsed["sources"]
        else:
            payload = [parsed]
    elif isinstance(parsed, list):
        payload = parsed
    else:
        raise ValueError("书源 JSON 必须是对象、数组，或带 sources 字段的对象")

    normalized_payload: List[Dict[str, Any]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError("第 {index} 个书源不是对象".format(index=index))
        normalized_payload.append(item)
    return normalized_payload
