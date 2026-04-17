from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .download_manager import ExtractionRules, NovelDownloadManager
from .rule_engine import RuleEngine, RuleEngineError
from .source_registry import SourceRegistry


@dataclass
class SourceDownloadConfig:
    max_workers: int = 4


class SourceDownloadService:
    def __init__(
        self,
        registry: SourceRegistry,
        engine: RuleEngine,
        manager: NovelDownloadManager,
        config: Optional[SourceDownloadConfig] = None,
    ):
        self.registry = registry
        self.engine = engine
        self.manager = manager
        self.config = config or SourceDownloadConfig()

    def create_book_job(
        self,
        source_id: str,
        book_url: str,
        book_name: str = "",
        output_filename: str = "",
    ) -> Dict[str, Any]:
        plan = self.preflight_book(source_id, book_url, book_name)
        return self.create_job_from_plan(plan, output_filename)

    def preflight_book(
        self,
        source_id: str,
        book_url: str,
        book_name: str = "",
    ) -> Dict[str, Any]:
        summary = self._get_supported_download_summary(source_id)
        source = self.registry.load_normalized_source(source_id)
        plan = self.engine.build_book_download_plan(source, book_url, book_name)
        toc = list(plan.get("toc") or [])
        return {
            "source_id": source_id,
            "source_name": summary.get("name") or source_id,
            "source_url": summary.get("source_url", ""),
            "book_url": plan.get("book_url", book_url),
            "toc_url": plan.get("toc_url", ""),
            "book_name": plan.get("book_name") or book_name or "未命名小说",
            "author": plan.get("author", ""),
            "intro": plan.get("intro", ""),
            "toc": toc,
            "toc_count": len(toc),
        }

    def create_job_from_plan(
        self,
        plan: Dict[str, Any],
        output_filename: str = "",
    ) -> Dict[str, Any]:
        source_id = str(plan.get("source_id") or "").strip()
        job = self.manager.create_job(
            str(plan.get("book_name") or "").strip() or "未命名小说",
            list(plan.get("toc") or []),
            ExtractionRules(content_regex=r"(?s)(.*)"),
            output_filename,
            str(plan.get("book_url") or "").strip(),
            metadata={
                "download_mode": "rule_based",
                "source_id": source_id,
                "source_name": str(plan.get("source_name") or "").strip(),
                "book_url": str(plan.get("book_url") or "").strip(),
                "toc_url": str(plan.get("toc_url") or "").strip(),
                "author": plan.get("author", ""),
                "intro": plan.get("intro", ""),
            },
        )
        return {
            "job_id": job["job_id"],
            "created": job["created"],
            "source_id": source_id,
            "source_name": str(plan.get("source_name") or "").strip(),
            "book_name": str(plan.get("book_name") or "").strip(),
            "book_url": str(plan.get("book_url") or "").strip(),
            "toc_url": str(plan.get("toc_url") or "").strip(),
            "toc_count": int(plan.get("toc_count", len(plan.get("toc") or [])) or 0),
            "preflight": {
                "source_id": source_id,
                "source_name": str(plan.get("source_name") or "").strip(),
                "book_name": str(plan.get("book_name") or "").strip(),
                "book_url": str(plan.get("book_url") or "").strip(),
                "toc_url": str(plan.get("toc_url") or "").strip(),
                "toc_count": int(plan.get("toc_count", len(plan.get("toc") or [])) or 0),
                "author": str(plan.get("author") or "").strip(),
                "intro": str(plan.get("intro") or "").strip(),
            },
            "status": self.manager.get_status(job["job_id"]),
        }

    def resume_book_job(self, job_id: str, auto_assemble: bool = True) -> Dict[str, Any]:
        manifest = self.manager.load_manifest(job_id)
        metadata = manifest.get("metadata") or {}
        if metadata.get("download_mode") != "rule_based":
            raise ValueError("任务不是书源规则下载任务，不能用 rule_based 恢复")

        source_id = metadata.get("source_id")
        if not source_id:
            raise ValueError("任务缺少 source_id，无法恢复")

        source = self.registry.load_normalized_source(source_id)
        missing = self.manager.get_missing_chapters(job_id)
        if not missing:
            if auto_assemble:
                return self.manager.assemble(
                    job_id,
                    self.manager.config.cleanup_journal_after_assemble,
                )
            return self.manager.get_status(job_id)

        self.manager.record_state(job_id, "downloading", missing_count=len(missing))
        errors = self._download_missing_chapters(source, job_id, missing)
        if errors:
            self.manager.record_state(job_id, "failed", error_count=errors)
            return self.manager.get_status(job_id)

        self.manager.record_state(job_id, "downloaded")
        if auto_assemble:
            return self.manager.assemble(
                job_id,
                self.manager.config.cleanup_journal_after_assemble,
            )
        return self.manager.get_status(job_id)

    def _download_missing_chapters(
        self,
        source: Dict[str, Any],
        job_id: str,
        missing: list[Dict[str, Any]],
    ) -> int:
        error_count = 0
        max_workers = min(max(1, self.config.max_workers), max(1, len(missing)))

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self._download_one_chapter, source, chapter): chapter
                for chapter in missing
            }
            for future in concurrent.futures.as_completed(future_map):
                chapter = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    error_count += 1
                    self.manager.append_download_error(
                        job_id,
                        chapter["index"],
                        chapter["title"],
                        chapter["url"],
                        str(exc),
                        1,
                    )
                    continue

                self.manager.append_downloaded_chapter(
                    job_id,
                    chapter["index"],
                    result["title"],
                    chapter["url"],
                    result["content"],
                    result.get("encoding", ""),
                    1,
                )
        return error_count

    def _download_one_chapter(self, source: Dict[str, Any], chapter: Dict[str, Any]) -> Dict[str, str]:
        try:
            return self.engine.fetch_chapter_content(
                source,
                chapter["url"],
                chapter["title"],
            )
        except RuleEngineError:
            raise
        except Exception as exc:
            raise RuleEngineError(str(exc))

    def _get_supported_download_summary(self, source_id: str) -> Dict[str, Any]:
        summary = self.registry.get_source_summary(source_id)
        if summary.get("supports_download"):
            return summary
        issues = "；".join(summary.get("issues") or []) or "当前书源不支持 route A TXT 下载"
        raise ValueError(
            "书源 {name} 当前不支持 TXT 下载：{issues}".format(
                name=summary.get("name") or source_id,
                issues=issues,
            )
        )
