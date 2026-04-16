from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

try:
    from jsonpath_ng.ext import parse as parse_jsonpath
except ImportError:
    parse_jsonpath = None

try:
    from parsel import Selector
except ImportError:
    Selector = None


class RuleEngineError(Exception):
    """Raised when the route-A rule engine cannot execute a source."""


@dataclass
class RuleEngineConfig:
    request_timeout: float = 20.0
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )


class RuleEngine:
    def __init__(self, config: RuleEngineConfig):
        self.config = config

    def search_books(
        self,
        source: Dict[str, Any],
        keyword: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        search_url = str(source.get("search_url") or "").strip()
        if not search_url:
            raise RuleEngineError("书源未提供 search_url")
        if not source.get("rule_search"):
            raise RuleEngineError("书源未提供 rule_search")

        rendered_url = self._render_template(
            search_url,
            {
                "key": keyword,
                "keyword": keyword,
                "keyEncoded": quote(keyword),
                "key_encoded": quote(keyword),
                "page": "1",
                "baseUrl": str(source.get("source_url") or ""),
                "base_url": str(source.get("source_url") or ""),
            },
        )
        response_text, final_url = self._fetch_text(
            rendered_url,
            headers=source.get("headers") or {},
        )
        payload_kind, payload = self._build_payload(response_text, final_url)
        results = self._extract_search_results(
            source,
            payload_kind,
            payload,
            final_url,
            keyword,
        )
        return results[: max(1, limit)]

    def _fetch_text(self, url: str, headers: Dict[str, str]) -> Tuple[str, str]:
        absolute_url = urljoin(headers.get("Referer", "") or "", url) if "://" not in url else url
        request_headers = {
            "User-Agent": self.config.user_agent,
        }
        for key, value in headers.items():
            if value:
                request_headers[str(key)] = str(value)

        request = Request(absolute_url, headers=request_headers)
        try:
            with urlopen(request, timeout=self.config.request_timeout) as response:
                body = response.read()
                final_url = getattr(response, "url", absolute_url)
                encoding = (
                    response.headers.get_content_charset()
                    or self._guess_encoding(body)
                    or "utf-8"
                )
        except HTTPError as exc:
            raise RuleEngineError("HTTP {code}: {reason}".format(code=exc.code, reason=exc.reason)) from exc
        except URLError as exc:
            raise RuleEngineError("网络错误: {reason}".format(reason=exc.reason)) from exc

        for candidate in self._candidate_encodings(encoding):
            try:
                return body.decode(candidate), final_url
            except UnicodeDecodeError:
                continue
        return body.decode("utf-8", errors="replace"), final_url

    def _guess_encoding(self, body: bytes) -> str:
        head = body[:4096].decode("ascii", errors="ignore")
        match = re.search(r"charset=['\"]?([a-zA-Z0-9_-]+)", head, flags=re.I)
        if match:
            return match.group(1)
        return "utf-8"

    def _candidate_encodings(self, primary: str) -> Iterable[str]:
        seen = set()
        for encoding in (primary, "utf-8", "gb18030", "gbk", "big5"):
            if encoding and encoding not in seen:
                seen.add(encoding)
                yield encoding

    def _build_payload(self, response_text: str, final_url: str) -> Tuple[str, Any]:
        try:
            return "json", json.loads(response_text)
        except Exception:
            pass

        if Selector is None:
            raise RuleEngineError(
                "当前环境缺少 parsel，无法解析 HTML 书源；请安装 requirements.txt 中的依赖"
            )
        return "html", Selector(text=response_text, base_url=final_url)

    def _extract_search_results(
        self,
        source: Dict[str, Any],
        payload_kind: str,
        payload: Any,
        final_url: str,
        keyword: str,
    ) -> List[Dict[str, Any]]:
        rule = source.get("rule_search") or {}
        list_rule = (
            rule.get("bookList")
            or rule.get("booklist")
            or rule.get("list")
            or rule.get("books")
            or rule.get("__default__")
            or "$"
        )
        items = self._select_many(payload_kind, payload, list_rule)
        items = self._flatten_result_items(items)
        results: List[Dict[str, Any]] = []

        for item in items:
            title = self._extract_scalar(
                payload_kind,
                item,
                rule.get("name") or rule.get("bookName") or rule.get("title") or "",
            )
            if not title:
                continue

            book_url = self._extract_scalar(
                payload_kind,
                item,
                rule.get("bookUrl") or rule.get("url") or rule.get("detailUrl") or "",
            )
            author = self._extract_scalar(payload_kind, item, rule.get("author") or "")
            cover_url = self._extract_scalar(
                payload_kind,
                item,
                rule.get("coverUrl") or rule.get("cover") or rule.get("img") or "",
            )
            intro = self._extract_scalar(
                payload_kind,
                item,
                rule.get("intro") or rule.get("introHtml") or rule.get("desc") or "",
            )
            kind = self._extract_scalar(payload_kind, item, rule.get("kind") or "")
            last_chapter = self._extract_scalar(
                payload_kind,
                item,
                rule.get("lastChapter") or rule.get("latestChapterTitle") or "",
            )
            word_count = self._extract_scalar(
                payload_kind,
                item,
                rule.get("wordCount") or rule.get("words") or "",
            )

            absolute_book_url = self._make_absolute_url(book_url, final_url, source)
            absolute_cover_url = self._make_absolute_url(cover_url, final_url, source)

            results.append(
                {
                    "source_id": source.get("source_id"),
                    "source_name": source.get("name"),
                    "title": title,
                    "author": author,
                    "book_url": absolute_book_url,
                    "cover_url": absolute_cover_url,
                    "intro": intro,
                    "kind": kind,
                    "last_chapter": last_chapter,
                    "word_count": word_count,
                    "match_keyword": keyword,
                }
            )
        return results

    def _flatten_result_items(self, items: List[Any]) -> List[Any]:
        flattened: List[Any] = []
        for item in items:
            if isinstance(item, list):
                flattened.extend(item)
            else:
                flattened.append(item)
        return flattened

    def _make_absolute_url(
        self,
        raw_url: str,
        final_url: str,
        source: Dict[str, Any],
    ) -> str:
        if not raw_url:
            return ""
        if "://" in raw_url:
            return raw_url
        base_url = str(source.get("source_url") or "")
        if raw_url.startswith("/") and base_url.startswith(("http://", "https://")):
            return urljoin(base_url, raw_url)
        return urljoin(final_url or base_url, raw_url)

    def _extract_scalar(self, payload_kind: str, payload: Any, rule_text: str) -> str:
        if not rule_text:
            return self._stringify(payload)
        values = self._select_many(payload_kind, payload, rule_text)
        if not values:
            return ""
        return self._stringify(values[0])

    def _select_many(self, payload_kind: str, payload: Any, rule_text: str) -> List[Any]:
        base_rule, cleaners = self._split_cleaners(str(rule_text or ""))
        if payload_kind == "json":
            values = self._select_json_many(payload, base_rule or "$")
        else:
            values = self._select_html_many(payload, base_rule or "*")
        cleaned: List[Any] = []
        for value in values:
            if cleaners:
                cleaned.append(self._apply_cleaners(self._stringify(value), cleaners))
            else:
                cleaned.append(value)
        return cleaned

    def _split_cleaners(self, rule_text: str) -> Tuple[str, List[Tuple[str, str]]]:
        if "##" not in rule_text:
            return rule_text.strip(), []
        parts = rule_text.split("##")
        base = parts[0].strip()
        cleaners: List[Tuple[str, str]] = []
        for index in range(1, len(parts), 2):
            pattern = parts[index]
            replacement = parts[index + 1] if index + 1 < len(parts) else ""
            cleaners.append((pattern, replacement))
        return base, cleaners

    def _apply_cleaners(self, value: str, cleaners: Sequence[Tuple[str, str]]) -> str:
        cleaned = value
        for pattern, replacement in cleaners:
            try:
                cleaned = re.sub(pattern, replacement, cleaned)
            except re.error:
                continue
        return cleaned.strip()

    def _select_json_many(self, payload: Any, rule_text: str) -> List[Any]:
        current: List[Any] = [payload]
        parts = [part.strip() for part in rule_text.split("&&") if part.strip()]
        for part in parts or ["$"]:
            next_values: List[Any] = []
            for node in current:
                next_values.extend(self._apply_json_step(node, part))
            current = next_values
        return current

    def _apply_json_step(self, node: Any, step: str) -> List[Any]:
        if step in ("$", "@", ""):
            return [node]
        if step in ("text", "@text"):
            return [self._stringify(node)]
        if parse_jsonpath is None:
            return self._fallback_json_lookup(node, step)

        expression = step
        if not expression.startswith("$"):
            if expression.startswith("["):
                expression = "$" + expression
            else:
                expression = "$." + expression.lstrip(".")

        try:
            compiled = parse_jsonpath(expression)
        except Exception:
            return self._fallback_json_lookup(node, step)

        return [match.value for match in compiled.find(node)]

    def _fallback_json_lookup(self, node: Any, step: str) -> List[Any]:
        parts = [part for part in step.strip("$.").split(".") if part]
        current = [node]
        for part in parts:
            next_values: List[Any] = []
            wildcard = part == "*"
            for item in current:
                if wildcard and isinstance(item, list):
                    next_values.extend(item)
                elif isinstance(item, dict) and part in item:
                    next_values.append(item[part])
                elif isinstance(item, list):
                    try:
                        next_values.append(item[int(part)])
                    except Exception:
                        continue
            current = next_values
        return current

    def _select_html_many(self, payload: Any, rule_text: str) -> List[Any]:
        if Selector is None:
            raise RuleEngineError(
                "当前环境缺少 parsel，无法解析 HTML 书源；请安装 requirements.txt 中的依赖"
            )

        current: List[Any] = [payload]
        parts = [part.strip() for part in rule_text.split("&&") if part.strip()]
        for part in parts:
            next_values: List[Any] = []
            for node in current:
                next_values.extend(self._apply_html_step(node, part))
            current = next_values
        return current

    def _apply_html_step(self, node: Any, step: str) -> List[Any]:
        if isinstance(node, str):
            if step in ("text", "@text", "@"):
                return [node]
            return []

        expression, attr = self._split_html_attr(step)
        if expression:
            selected = self._html_select(node, expression)
        else:
            selected = [node]

        if not attr:
            return selected

        values: List[Any] = []
        for item in selected:
            if isinstance(item, str):
                values.append(item)
                continue
            if attr == "text":
                values.append(self._node_text(item))
            elif attr == "html":
                values.append(item.get())
            else:
                values.append(item.attrib.get(attr, ""))
        return values

    def _split_html_attr(self, step: str) -> Tuple[str, str]:
        if step in ("text", "@text"):
            return "", "text"
        if step in ("html", "@html"):
            return "", "html"
        if step.endswith("@text"):
            return step[:-5], "text"
        if step.endswith("@html"):
            return step[:-5], "html"
        if "@" in step and not step.lstrip().startswith(("/", ".", "xpath:")):
            expression, attr = step.rsplit("@", 1)
            return expression.strip(), attr.strip()
        return step, ""

    def _html_select(self, node: Any, expression: str) -> List[Any]:
        if expression.startswith("xpath:"):
            return list(node.xpath(expression[len("xpath:") :]))
        if expression.startswith(("//", ".//", "./", "/")):
            return list(node.xpath(expression))
        return list(node.css(expression))

    def _node_text(self, node: Any) -> str:
        if isinstance(node, str):
            return node.strip()
        try:
            text = node.xpath("string(.)").get(default="")
        except Exception:
            text = node.get() or ""
        return self._normalize_text(text)

    def _stringify(self, value: Any) -> str:
        if isinstance(value, str):
            return self._normalize_text(value)
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if hasattr(value, "get"):
            return self._normalize_text(value.get() or "")
        return self._normalize_text(str(value))

    def _normalize_text(self, value: str) -> str:
        text = unescape(value or "")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _render_template(self, template: str, variables: Dict[str, str]) -> str:
        def replacer(match: re.Match) -> str:
            key = match.group(1).strip()
            return variables.get(key, "")

        return re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", replacer, template)
