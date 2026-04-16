from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List


REGISTRY_SCHEMA_VERSION = 1


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


def normalize_book_source(raw_source: Dict[str, Any]) -> Dict[str, Any]:
    source_name = _clean_text(
        raw_source.get("bookSourceName")
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
        "group": _clean_text(raw_source.get("bookSourceGroup") or raw_source.get("group")),
        "enabled": enabled,
        "search_url": _clean_text(raw_source.get("searchUrl")),
        "explore_url": _clean_text(raw_source.get("exploreUrl")),
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
    book_source_type: int
    has_rule_search: bool
    has_rule_book_info: bool
    has_rule_toc: bool
    has_rule_content: bool
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_source_summary(normalized: Dict[str, Any], updated_at: float) -> SourceSummary:
    return SourceSummary(
        source_id=normalized["source_id"],
        name=normalized["name"],
        source_url=normalized["source_url"],
        enabled=bool(normalized["enabled"]),
        group=normalized.get("group", ""),
        search_url=normalized.get("search_url", ""),
        book_source_type=int(normalized.get("book_source_type", 0) or 0),
        has_rule_search=bool(normalized.get("rule_search")),
        has_rule_book_info=bool(normalized.get("rule_book_info")),
        has_rule_toc=bool(normalized.get("rule_toc")),
        has_rule_content=bool(normalized.get("rule_content")),
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
