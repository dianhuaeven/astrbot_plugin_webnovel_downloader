from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
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
class RequestSpec:
    url: str
    method: str = "GET"
    body: bytes | None = None
    request_encoding: str = "utf-8"
    headers: Dict[str, str] | None = None


@dataclass
class RuleEngineConfig:
    request_timeout: float = 20.0
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )


class RuleEngine:
    _COMMON_HTML_ATTRS = {
        "href",
        "src",
        "alt",
        "title",
        "value",
        "content",
        "class",
        "id",
        "style",
        "text",
        "html",
    }

    def __init__(self, config: RuleEngineConfig):
        self.config = config
        self._cleaner_cache: Dict[str, List[Tuple[str, str]]] = {}

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
        request_spec = self._build_request_spec(source, rendered_url)
        response_text, final_url = self._fetch_text(
            request_spec.url,
            headers=self._merge_headers(source.get("headers") or {}, request_spec.headers or {}),
            method=request_spec.method,
            body=request_spec.body,
            request_encoding=request_spec.request_encoding,
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

    def build_book_download_plan(
        self,
        source: Dict[str, Any],
        book_url: str,
        fallback_title: str = "",
    ) -> Dict[str, Any]:
        detail_text, detail_final_url = self._fetch_text(book_url, headers=source.get("headers") or {})
        payload_kind, payload = self._build_payload(detail_text, detail_final_url)
        book_info_rule = source.get("rule_book_info") or {}
        toc_rule = source.get("rule_toc") or {}

        title = self._extract_scalar(
            payload_kind,
            payload,
            book_info_rule.get("name")
            or book_info_rule.get("bookName")
            or book_info_rule.get("title")
            or "",
        ) or fallback_title
        author = self._extract_scalar(payload_kind, payload, book_info_rule.get("author") or "")
        intro = self._extract_scalar(
            payload_kind,
            payload,
            book_info_rule.get("intro") or book_info_rule.get("desc") or "",
        )
        toc_url = self._extract_scalar(
            payload_kind,
            payload,
            book_info_rule.get("tocUrl")
            or book_info_rule.get("chapterUrl")
            or book_info_rule.get("catalogUrl")
            or book_info_rule.get("listUrl")
            or "",
        )
        toc_page_url = self._make_absolute_url(toc_url, detail_final_url, source) or detail_final_url
        toc = self.fetch_chapter_list(source, toc_page_url, toc_rule)
        if not toc:
            raise RuleEngineError("未解析到目录，请检查 ruleToc")
        return {
            "book_url": detail_final_url,
            "toc_url": toc_page_url,
            "book_name": title or fallback_title or "未命名小说",
            "author": author,
            "intro": intro,
            "toc": toc,
        }

    def fetch_chapter_list(
        self,
        source: Dict[str, Any],
        toc_url: str,
        toc_rule: Dict[str, Any],
        max_pages: int = 5,
    ) -> List[Dict[str, Any]]:
        if not toc_rule:
            raise RuleEngineError("书源未提供 rule_toc")

        current_url = toc_url
        visited = set()
        chapters: List[Dict[str, Any]] = []
        for _ in range(max_pages):
            if not current_url or current_url in visited:
                break
            visited.add(current_url)
            page_text, final_url = self._fetch_text(current_url, headers=source.get("headers") or {})
            payload_kind, payload = self._build_payload(page_text, final_url)
            list_rule = (
                toc_rule.get("chapterList")
                or toc_rule.get("list")
                or toc_rule.get("tocList")
                or toc_rule.get("__default__")
                or "$"
            )
            items = self._flatten_result_items(self._select_many(payload_kind, payload, list_rule))
            for item in items:
                title = self._extract_scalar(
                    payload_kind,
                    item,
                    toc_rule.get("chapterName")
                    or toc_rule.get("name")
                    or toc_rule.get("title")
                    or toc_rule.get("text")
                    or "",
                )
                url = self._extract_scalar(
                    payload_kind,
                    item,
                    toc_rule.get("chapterUrl")
                    or toc_rule.get("url")
                    or toc_rule.get("link")
                    or "",
                )
                absolute_url = self._make_absolute_url(url, final_url, source)
                if not title or not absolute_url:
                    continue
                chapters.append(
                    {
                        "title": title,
                        "url": absolute_url,
                    }
                )

            next_toc_url = self._extract_scalar(
                payload_kind,
                payload,
                toc_rule.get("nextTocUrl") or toc_rule.get("nextUrl") or "",
            )
            current_url = self._make_absolute_url(next_toc_url, final_url, source) if next_toc_url else ""

        deduped: List[Dict[str, Any]] = []
        seen_urls = set()
        for chapter in chapters:
            if chapter["url"] in seen_urls:
                continue
            seen_urls.add(chapter["url"])
            deduped.append(chapter)
        return [
            {
                "index": index,
                "title": chapter["title"],
                "url": chapter["url"],
            }
            for index, chapter in enumerate(deduped)
        ]

    def fetch_chapter_content(
        self,
        source: Dict[str, Any],
        chapter_url: str,
        fallback_title: str = "",
        max_pages: int = 5,
    ) -> Dict[str, str]:
        content_rule = source.get("rule_content") or {}
        if not content_rule:
            raise RuleEngineError("书源未提供 rule_content")

        current_url = chapter_url
        visited = set()
        segments: List[str] = []
        chosen_title = fallback_title
        final_encoding = ""

        for _ in range(max_pages):
            if not current_url or current_url in visited:
                break
            visited.add(current_url)
            page_text, final_url = self._fetch_text(current_url, headers=source.get("headers") or {})
            payload_kind, payload = self._build_payload(page_text, final_url)
            title = self._extract_scalar(
                payload_kind,
                payload,
                content_rule.get("title")
                or content_rule.get("chapterName")
                or content_rule.get("name")
                or "",
            )
            content = self._extract_scalar(
                payload_kind,
                payload,
                content_rule.get("content")
                or content_rule.get("text")
                or content_rule.get("body")
                or content_rule.get("__default__")
                or "",
            )
            if title:
                chosen_title = title
            if content:
                segments.append(content)

            next_content_url = self._extract_scalar(
                payload_kind,
                payload,
                content_rule.get("nextContentUrl") or content_rule.get("nextUrl") or "",
            )
            if not next_content_url:
                break
            current_url = self._make_absolute_url(next_content_url, final_url, source)

        merged_content = "\n\n".join([segment for segment in segments if segment.strip()])
        merged_content = self.apply_content_cleaners(source, merged_content)
        if not merged_content:
            raise RuleEngineError("未解析到正文，请检查 ruleContent")
        return {
            "title": chosen_title or fallback_title,
            "content": merged_content,
            "encoding": final_encoding,
        }

    def apply_content_cleaners(self, source: Dict[str, Any], content: str) -> str:
        cleaned = content
        remote_cleaners = self._load_remote_cleaners(source)
        if remote_cleaners:
            cleaned = self._apply_cleaners(cleaned, remote_cleaners)
        cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _fetch_text(
        self,
        url: str,
        headers: Dict[str, str],
        method: str = "GET",
        body: bytes | None = None,
        request_encoding: str = "utf-8",
    ) -> Tuple[str, str]:
        absolute_url = urljoin(headers.get("Referer", "") or "", url) if "://" not in url else url
        normalized_url = self._normalize_request_url(
            absolute_url,
            request_encoding or "utf-8",
        )
        request_headers = {
            "User-Agent": self.config.user_agent,
        }
        for key, value in headers.items():
            if value:
                request_headers[str(key)] = str(value)
        if body is not None and not self._has_content_type(request_headers):
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = Request(
            normalized_url,
            headers=request_headers,
            data=body,
            method=(method or "GET").upper(),
        )
        try:
            with urlopen(request, timeout=self.config.request_timeout) as response:
                body = response.read()
                final_url = getattr(response, "url", normalized_url)
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

    def _build_request_spec(self, source: Dict[str, Any], rendered_url: str) -> RequestSpec:
        base_url, options = self._split_request_options(rendered_url)
        request_encoding = str(
            options.get("charset")
            or options.get("encoding")
            or "utf-8"
        ).strip() or "utf-8"
        method = str(options.get("method") or "GET").strip().upper() or "GET"
        headers = self._normalize_request_headers(options.get("headers"))
        body = self._encode_request_body(
            options.get("body") or options.get("data"),
            request_encoding,
        )
        if body is not None and method == "GET":
            method = "POST"
        absolute_url = self._make_absolute_url(
            base_url,
            str(source.get("source_url") or ""),
            source,
        )
        return RequestSpec(
            url=absolute_url,
            method=method,
            body=body,
            request_encoding=request_encoding,
            headers=headers,
        )

    def _split_request_options(self, raw_text: str) -> Tuple[str, Dict[str, Any]]:
        text = str(raw_text or "").strip()
        if not text:
            return "", {}
        for index in range(len(text) - 1, -1, -1):
            if text[index] not in "{[":
                continue
            prefix = text[:index].rstrip()
            if not prefix.endswith(","):
                continue
            suffix = text[index:]
            try:
                parsed = json.loads(suffix)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return prefix[:-1].rstrip(), parsed
        return text, {}

    def _normalize_request_headers(self, raw_headers: Any) -> Dict[str, str]:
        if not isinstance(raw_headers, dict):
            return {}
        result: Dict[str, str] = {}
        for key, value in raw_headers.items():
            if value is None:
                continue
            result[str(key)] = str(value)
        return result

    def _merge_headers(
        self,
        base_headers: Dict[str, str],
        extra_headers: Dict[str, str],
    ) -> Dict[str, str]:
        merged: Dict[str, str] = {}
        for key, value in base_headers.items():
            if value:
                merged[str(key)] = str(value)
        for key, value in extra_headers.items():
            if value:
                merged[str(key)] = str(value)
        return merged

    def _encode_request_body(
        self,
        body: Any,
        request_encoding: str,
    ) -> bytes | None:
        if body is None:
            return None
        if isinstance(body, bytes):
            return body
        if isinstance(body, (dict, list)):
            text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            return text.encode(request_encoding, errors="strict")
        text = str(body).strip()
        if not text:
            return None
        encoded = quote(text, safe="=&%+/:;?,@[]{}", encoding=request_encoding)
        return encoded.encode("ascii")

    def _normalize_request_url(self, url: str, request_encoding: str) -> str:
        split = urlsplit(url)
        path = quote(split.path or "", safe="/%:@", encoding=request_encoding)
        query = quote(split.query or "", safe="=&;%:+,/?%@[]", encoding=request_encoding)
        fragment = quote(split.fragment or "", safe="%:@", encoding=request_encoding)
        return urlunsplit((split.scheme, split.netloc, path, query, fragment))

    def _has_content_type(self, headers: Dict[str, str]) -> bool:
        return any(str(key).lower() == "content-type" for key in headers)

    def _load_remote_cleaners(self, source: Dict[str, Any]) -> List[Tuple[str, str]]:
        clean_url = str(source.get("clean_rule_url") or "").strip()
        if not clean_url:
            return []
        if clean_url in self._cleaner_cache:
            return self._cleaner_cache[clean_url]
        try:
            text, _ = self._fetch_text(clean_url, headers=source.get("headers") or {})
        except Exception:
            self._cleaner_cache[clean_url] = []
            return []
        cleaners = self._parse_remote_cleaners(text)
        self._cleaner_cache[clean_url] = cleaners
        return cleaners

    def _parse_remote_cleaners(self, raw_text: str) -> List[Tuple[str, str]]:
        text = raw_text.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None

        cleaners: List[Tuple[str, str]] = []
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    pattern = str(item.get("regex") or item.get("pattern") or "").strip()
                    replacement = str(item.get("replacement") or item.get("replace") or "")
                    if pattern:
                        cleaners.append((pattern, replacement))
        elif isinstance(parsed, dict):
            rules = parsed.get("rules") or parsed.get("cleaners")
            if isinstance(rules, list):
                for item in rules:
                    if isinstance(item, dict):
                        pattern = str(item.get("regex") or item.get("pattern") or "").strip()
                        replacement = str(item.get("replacement") or item.get("replace") or "")
                        if pattern:
                            cleaners.append((pattern, replacement))

        if cleaners:
            return cleaners

        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("##"):
                _, cleaner_list = self._split_cleaners(line)
                cleaners.extend(cleaner_list)
                continue
            if "##" in line:
                parts = line.split("##", 1)
                pattern = parts[0].strip()
                replacement = parts[1] if len(parts) > 1 else ""
                if pattern:
                    cleaners.append((pattern, replacement))
        return cleaners

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
            return ""
        base_rule, cleaners = self._split_cleaners(str(rule_text or ""))
        if "{{" in base_rule and "}}" in base_rule:
            rendered = self._render_rule_template(payload_kind, payload, base_rule)
            if cleaners:
                rendered = self._apply_cleaners(rendered, cleaners)
            return self._normalize_text(rendered)
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

        expressions, attr = self._split_html_step(step)
        selected = [node]
        for expression in expressions:
            next_selected: List[Any] = []
            for current in selected:
                next_selected.extend(self._html_select(current, expression))
            selected = next_selected

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

    def _split_html_step(self, step: str) -> Tuple[List[str], str]:
        if step in ("text", "@text"):
            return [], "text"
        if step in ("html", "@html"):
            return [], "html"
        if step.startswith("@") and len(step) > 1:
            return [], step[1:].strip()

        parts = [part.strip() for part in str(step or "").split("@") if part.strip()]
        if not parts:
            return [], ""
        attr = ""
        if len(parts) > 1 and self._is_html_attr_token(parts[-1]):
            attr = parts[-1]
            parts = parts[:-1]
        return parts, attr

    def _is_html_attr_token(self, token: str) -> bool:
        if token in self._COMMON_HTML_ATTRS:
            return True
        if token.startswith(("data-", "aria-")):
            return True
        return False

    def _html_select(self, node: Any, expression: str) -> List[Any]:
        index = None
        selector_expression = expression
        if not expression.startswith(("xpath:", "//", ".//", "./", "/")):
            selector_expression, index = self._split_html_index(expression)
        if expression.startswith("xpath:"):
            selected = list(node.xpath(expression[len("xpath:") :]))
        elif expression.startswith(("//", ".//", "./", "/")):
            selected = list(node.xpath(expression))
        else:
            selected = list(node.css(selector_expression))
        if index is None:
            return selected
        if not selected:
            return []
        if index < 0:
            index += len(selected)
        if index < 0 or index >= len(selected):
            return []
        return [selected[index]]

    def _split_html_index(self, expression: str) -> Tuple[str, int | None]:
        match = re.match(r"^(.*)\.(-?\d+)$", expression.strip())
        if not match:
            return expression, None
        base_expression = match.group(1).strip()
        if not base_expression:
            return expression, None
        try:
            return base_expression, int(match.group(2))
        except ValueError:
            return expression, None

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

    def _render_rule_template(self, payload_kind: str, payload: Any, template: str) -> str:
        def replacer(match: re.Match) -> str:
            expression = match.group(1).strip()
            if not expression:
                return ""
            values = []
            if payload_kind == "json":
                values = self._select_json_many(payload, expression)
            else:
                values = self._select_html_many(payload, expression)
            if not values:
                return ""
            return self._stringify(values[0])

        return re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", replacer, template)
