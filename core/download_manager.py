from __future__ import annotations

import concurrent.futures
import hashlib
import html
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCHEMA_VERSION = 1


def sanitize_filename(name: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", name or "").strip().strip(".")
    return value or "book"


def flags_from_string(value: str) -> int:
    mapping = {
        "i": re.IGNORECASE,
        "m": re.MULTILINE,
        "s": re.DOTALL,
    }
    flags = 0
    for char in (value or "is").lower():
        flags |= mapping.get(char, 0)
    return flags


def compact_json(data: Dict[str, Any]) -> bytes:
    return (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


@dataclass
class ExtractionRules:
    content_regex: str
    title_regex: str = ""
    content_flags: str = "is"
    title_flags: str = "is"
    strip_html_tags: bool = True
    html_unescape: bool = True
    normalize_whitespace: bool = True


@dataclass
class RuntimeConfig:
    max_workers: int = 6
    request_timeout: float = 20.0
    max_retries: int = 3
    retry_backoff: float = 1.6
    journal_fsync: bool = False
    default_encoding: str = ""
    preview_chars: int = 4000
    auto_assemble: bool = True
    cleanup_journal_after_assemble: bool = False
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )


class NovelDownloadManager:
    def __init__(self, base_dir: Union[str, Path], config: RuntimeConfig):
        self.base_dir = Path(base_dir)
        self.jobs_dir = self.base_dir / "jobs"
        self.output_dir = self.base_dir / "downloads"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.config = config
        self._write_lock = threading.Lock()

    def fetch_preview(
        self, url: str, encoding: str = "", max_chars: Optional[int] = None
    ) -> Dict[str, Any]:
        html_text, resolved_encoding = self._fetch_text(url, encoding=encoding)
        title = self._extract_html_title(html_text)
        text_preview = self._clean_text(html_text)
        return {
            "url": url,
            "encoding": resolved_encoding,
            "title": title,
            "html_preview": html_text[: max_chars or self.config.preview_chars],
            "text_preview": text_preview[: max_chars or self.config.preview_chars],
        }

    def create_job(
        self,
        book_name: str,
        toc: List[Dict[str, str]],
        rules: ExtractionRules,
        output_filename: str = "",
        source_url: str = "",
        encoding: str = "",
    ) -> Dict[str, Any]:
        normalized_toc = self._normalize_toc(toc)
        job_id = self._build_job_id(book_name, normalized_toc)
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        journal_path = job_dir / "job.jsonl"

        if journal_path.exists():
            status = self.get_status(job_id)
            return {
                "job_id": job_id,
                "created": False,
                "status": status,
            }

        output_name = sanitize_filename(output_filename or book_name) + ".txt"
        manifest = {
            "kind": "manifest",
            "schema_version": SCHEMA_VERSION,
            "created_at": time.time(),
            "job_id": job_id,
            "book_name": book_name,
            "source_url": source_url,
            "encoding": encoding or self.config.default_encoding,
            "output_filename": output_name,
            "rules": {
                "content_regex": rules.content_regex,
                "title_regex": rules.title_regex,
                "content_flags": rules.content_flags,
                "title_flags": rules.title_flags,
                "strip_html_tags": rules.strip_html_tags,
                "html_unescape": rules.html_unescape,
                "normalize_whitespace": rules.normalize_whitespace,
            },
            "chapters": normalized_toc,
        }
        self._append_record(journal_path, manifest)
        self._append_record(
            journal_path,
            {
                "kind": "state",
                "state": "created",
                "at": time.time(),
            },
        )
        return {
            "job_id": job_id,
            "created": True,
            "status": self.get_status(job_id),
        }

    def download_missing(self, job_id: str) -> Dict[str, Any]:
        manifest, replay = self._replay_job(job_id)
        if not manifest:
            raise ValueError(f"未找到任务 {job_id}")

        missing = [
            chapter
            for chapter in manifest["chapters"]
            if chapter["index"] not in replay["completed_indices"]
        ]
        journal_path = self._journal_path(job_id)
        if not missing:
            return self.get_status(job_id)

        self._append_record(
            journal_path,
            {
                "kind": "state",
                "state": "downloading",
                "at": time.time(),
                "missing_count": len(missing),
            },
        )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, self.config.max_workers)
        ) as executor:
            futures = [
                executor.submit(self._download_one, manifest, chapter) for chapter in missing
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()

        self._append_record(
            journal_path,
            {
                "kind": "state",
                "state": "downloaded",
                "at": time.time(),
            },
        )
        return self.get_status(job_id)

    def assemble(self, job_id: str, cleanup_journal: bool = False) -> Dict[str, Any]:
        manifest, replay = self._replay_job(job_id)
        if not manifest:
            raise ValueError(f"未找到任务 {job_id}")

        total = len(manifest["chapters"])
        if len(replay["chapter_offsets"]) != total:
            missing = [
                chapter["index"]
                for chapter in manifest["chapters"]
                if chapter["index"] not in replay["chapter_offsets"]
            ]
            raise ValueError(f"仍有章节未完成，缺失索引: {missing[:20]}")

        journal_path = self._journal_path(job_id)
        final_path = self.output_dir / manifest["output_filename"]
        tmp_path = final_path.with_suffix(final_path.suffix + ".part")

        self._append_record(
            journal_path,
            {
                "kind": "state",
                "state": "assembling",
                "at": time.time(),
                "target": str(final_path),
            },
        )

        with open(tmp_path, "w", encoding="utf-8", newline="\n") as output_handle:
            output_handle.write(f"书名：{manifest['book_name']}\n")
            output_handle.write(f"章节数：{total}\n\n")
            with open(journal_path, "rb") as journal_handle:
                for chapter in manifest["chapters"]:
                    offset = replay["chapter_offsets"][chapter["index"]]
                    journal_handle.seek(offset)
                    raw_line = journal_handle.readline()
                    record = json.loads(raw_line.decode("utf-8"))
                    output_handle.write(f"{record['title']}\n\n")
                    output_handle.write(record["content"])
                    output_handle.write("\n\n")
            output_handle.flush()
            os.fsync(output_handle.fileno())

        os.replace(tmp_path, final_path)
        self._append_record(
            journal_path,
            {
                "kind": "state",
                "state": "assembled",
                "at": time.time(),
                "target": str(final_path),
            },
        )
        status = self.get_status(job_id)
        if cleanup_journal:
            os.remove(journal_path)
            status["journal_path"] = "(deleted after assemble)"
        return status

    def get_status(self, job_id: str) -> Dict[str, Any]:
        manifest, replay = self._replay_job(job_id)
        if not manifest:
            raise ValueError(f"未找到任务 {job_id}")

        total = len(manifest["chapters"])
        completed = len(replay["completed_indices"])
        failed = len(replay["latest_errors"])
        status = {
            "job_id": job_id,
            "book_name": manifest["book_name"],
            "state": replay["last_state"] or "created",
            "total_chapters": total,
            "completed_chapters": completed,
            "failed_chapters": failed,
            "missing_chapters": max(0, total - completed),
            "output_filename": manifest["output_filename"],
            "output_path": str(self.output_dir / manifest["output_filename"]),
            "journal_path": str(self._journal_path(job_id)),
            "latest_errors": list(replay["latest_errors"].values())[:10],
            "corrupt_lines": replay["corrupt_lines"],
        }
        return status

    def list_jobs(self) -> List[Dict[str, Any]]:
        jobs: List[Dict[str, Any]] = []
        for job_dir in sorted(self.jobs_dir.iterdir()):
            if not job_dir.is_dir():
                continue
            journal_path = job_dir / "job.jsonl"
            if not journal_path.exists():
                continue
            try:
                jobs.append(self.get_status(job_dir.name))
            except Exception as exc:
                jobs.append(
                    {
                        "job_id": job_dir.name,
                        "state": "broken",
                        "error": str(exc),
                    }
                )
        return jobs

    def record_state(self, job_id: str, state: str, **extra: Any) -> None:
        journal_path = self._journal_path(job_id)
        if not journal_path.exists():
            return
        payload = {
            "kind": "state",
            "state": state,
            "at": time.time(),
        }
        payload.update(extra)
        self._append_record(journal_path, payload)

    def _download_one(self, manifest: Dict[str, Any], chapter: Dict[str, Any]) -> None:
        rules = ExtractionRules(**manifest["rules"])
        journal_path = self.jobs_dir / manifest["job_id"] / "job.jsonl"
        attempts = max(1, self.config.max_retries)
        encoding = manifest.get("encoding") or self.config.default_encoding

        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                html_text, resolved_encoding = self._fetch_text(
                    chapter["url"], encoding=encoding
                )
                title, content = self._extract_chapter(
                    html_text, chapter["title"], rules
                )
                self._append_record(
                    journal_path,
                    {
                        "kind": "chapter",
                        "index": chapter["index"],
                        "title": title,
                        "url": chapter["url"],
                        "encoding": resolved_encoding,
                        "attempt": attempt,
                        "saved_at": time.time(),
                        "content": content,
                    },
                )
                return
            except Exception as exc:
                last_error = str(exc)
                self._append_record(
                    journal_path,
                    {
                        "kind": "error",
                        "index": chapter["index"],
                        "title": chapter["title"],
                        "url": chapter["url"],
                        "attempt": attempt,
                        "saved_at": time.time(),
                        "error": last_error,
                    },
                )
                if attempt < attempts:
                    time.sleep(self.config.retry_backoff ** attempt)

        raise RuntimeError(
            f"章节下载失败 index={chapter['index']} title={chapter['title']} error={last_error}"
        )

    def _extract_chapter(
        self, html_text: str, fallback_title: str, rules: ExtractionRules
    ) -> Tuple[str, str]:
        content_match = re.search(
            rules.content_regex, html_text, flags=flags_from_string(rules.content_flags)
        )
        if not content_match:
            raise ValueError("未匹配到正文，请调整 content_regex")

        content = self._best_group(content_match)
        title = fallback_title
        if rules.title_regex:
            title_match = re.search(
                rules.title_regex, html_text, flags=flags_from_string(rules.title_flags)
            )
            if title_match:
                title = self._best_group(title_match)

        cleaned_content = self._clean_text(
            content,
            strip_html_tags=rules.strip_html_tags,
            html_unescape_enabled=rules.html_unescape,
            normalize_whitespace=rules.normalize_whitespace,
        )
        cleaned_title = self._clean_text(title, strip_html_tags=True).strip()
        if not cleaned_content:
            raise ValueError("正文提取结果为空，请调整 content_regex")
        return cleaned_title or fallback_title, cleaned_content

    def _build_job_id(self, book_name: str, toc: List[Dict[str, str]]) -> str:
        digest = hashlib.sha1(
            json.dumps(
                {
                    "book_name": book_name,
                    "toc": toc,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:12]
        prefix = sanitize_filename(book_name)[:24]
        return f"{prefix}-{digest}"

    def _normalize_toc(self, toc: List[Dict[str, str]]) -> List[Dict[str, str]]:
        if not isinstance(toc, list) or not toc:
            raise ValueError("toc_json 必须是非空 JSON 数组")

        normalized: List[Dict[str, str]] = []
        for index, chapter in enumerate(toc):
            if not isinstance(chapter, dict):
                raise ValueError(f"第 {index} 项不是对象")
            title = str(chapter.get("title") or "").strip()
            url = str(chapter.get("url") or "").strip()
            if not title or not url:
                raise ValueError(f"第 {index} 章缺少 title 或 url")
            normalized.append(
                {
                    "index": index,
                    "title": title,
                    "url": url,
                }
            )
        return normalized

    def _fetch_text(self, url: str, encoding: str = "") -> Tuple[str, str]:
        request = Request(
            url,
            headers={
                "User-Agent": self.config.user_agent,
            },
        )
        try:
            with urlopen(request, timeout=self.config.request_timeout) as response:
                body = response.read()
                guessed = (
                    encoding
                    or response.headers.get_content_charset()
                    or self._guess_encoding(body)
                )
        except HTTPError as exc:
            raise ValueError(f"HTTP {exc.code}: {exc.reason}") from exc
        except URLError as exc:
            raise ValueError(f"网络错误: {exc.reason}") from exc

        for candidate in self._candidate_encodings(guessed):
            try:
                return body.decode(candidate), candidate
            except UnicodeDecodeError:
                continue
        return body.decode("utf-8", errors="replace"), "utf-8"

    def _guess_encoding(self, body: bytes) -> str:
        head = body[:4096].decode("ascii", errors="ignore")
        match = re.search(r"charset=['\"]?([a-zA-Z0-9_-]+)", head, flags=re.I)
        if match:
            return match.group(1)
        return "utf-8"

    def _candidate_encodings(self, primary: str) -> Iterable[str]:
        candidates = [primary, "utf-8", "gb18030", "gbk", "big5"]
        seen = set()
        for item in candidates:
            if item and item not in seen:
                seen.add(item)
                yield item

    def _extract_html_title(self, html_text: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", html_text, flags=re.I | re.S)
        if not match:
            return ""
        return self._clean_text(match.group(1), strip_html_tags=True).strip()

    def _clean_text(
        self,
        value: str,
        strip_html_tags: bool = True,
        html_unescape_enabled: bool = True,
        normalize_whitespace: bool = True,
    ) -> str:
        text = value.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"(?is)<script.*?</script>", "", text)
        text = re.sub(r"(?is)<style.*?</style>", "", text)
        if strip_html_tags:
            text = re.sub(r"(?i)<br\s*/?>", "\n", text)
            text = re.sub(r"(?i)</p\s*>", "\n", text)
            text = re.sub(r"(?i)</div\s*>", "\n", text)
            text = re.sub(r"(?i)</li\s*>", "\n", text)
            text = re.sub(r"(?i)</tr\s*>", "\n", text)
            text = re.sub(r"(?is)<[^>]+>", "", text)
        if html_unescape_enabled:
            text = html.unescape(text)
        text = text.replace("\u00a0", " ").replace("\u3000", "  ")
        if normalize_whitespace:
            text = re.sub(r"[ \t]+\n", "\n", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()

    def _best_group(self, match: re.Match) -> str:
        if match.lastindex:
            for index in range(1, match.lastindex + 1):
                value = match.group(index)
                if value:
                    return value
        return match.group(0)

    def _append_record(self, journal_path: Path, record: Dict[str, Any]) -> None:
        payload = compact_json(record)
        with self._write_lock:
            with open(journal_path, "ab") as handle:
                handle.write(payload)
                handle.flush()
                if self.config.journal_fsync:
                    os.fsync(handle.fileno())

    def _journal_path(self, job_id: str) -> Path:
        return self.jobs_dir / job_id / "job.jsonl"

    def _replay_job(self, job_id: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        journal_path = self._journal_path(job_id)
        if not journal_path.exists():
            return None, {}

        manifest = None
        chapter_offsets: Dict[int, int] = {}
        completed_indices = set()
        latest_errors: Dict[int, Dict[str, Any]] = {}
        last_state = ""
        corrupt_lines = 0

        with open(journal_path, "rb") as handle:
            while True:
                offset = handle.tell()
                raw_line = handle.readline()
                if not raw_line:
                    break
                if not raw_line.endswith(b"\n"):
                    break
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except Exception:
                    corrupt_lines += 1
                    continue

                kind = record.get("kind")
                if kind == "manifest" and manifest is None:
                    manifest = record
                elif kind == "chapter":
                    index = int(record["index"])
                    chapter_offsets[index] = offset
                    completed_indices.add(index)
                    latest_errors.pop(index, None)
                elif kind == "error":
                    latest_errors[int(record["index"])] = {
                        "index": record["index"],
                        "title": record.get("title", ""),
                        "error": record.get("error", ""),
                        "attempt": record.get("attempt", 0),
                    }
                elif kind == "state":
                    last_state = str(record.get("state") or "")

        return manifest, {
            "chapter_offsets": chapter_offsets,
            "completed_indices": completed_indices,
            "latest_errors": latest_errors,
            "last_state": last_state,
            "corrupt_lines": corrupt_lines,
        }
