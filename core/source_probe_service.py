from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class SourceProbeServiceConfig:
    max_workers: int = 2
    probe_keywords: tuple[str, ...] = (
        "诡秘之主",
        "斗破苍穹",
        "凡人修仙传",
    )


class SourceProbeService:
    def __init__(
        self,
        registry: Any,
        engine: Any,
        health_store: Any,
        source_profile_service: Any = None,
        config: SourceProbeServiceConfig | None = None,
    ):
        if isinstance(source_profile_service, SourceProbeServiceConfig) and config is None:
            config = source_profile_service
            source_profile_service = None
        self.registry = registry
        self.engine = engine
        self.health_store = health_store
        self.source_profile_service = source_profile_service
        self.config = config or SourceProbeServiceConfig()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._state_lock = threading.Lock()
        self._idle_condition = threading.Condition(self._state_lock)
        self._queued_ids: set[str] = set()
        self._active_ids: set[str] = set()
        self._workers_started = False
        self._workers: list[threading.Thread] = []
        self._shutdown = False

    def enqueue_sources(self, source_ids: Iterable[str]) -> dict[str, int]:
        normalized_ids = []
        seen: set[str] = set()
        for source_id in source_ids:
            normalized = str(source_id or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            normalized_ids.append(normalized)

        queued_count = 0
        with self._idle_condition:
            if normalized_ids and not self._workers_started:
                self._start_workers_locked()
            for source_id in normalized_ids:
                if source_id in self._queued_ids or source_id in self._active_ids:
                    continue
                self._queued_ids.add(source_id)
                self._queue.put(source_id)
                queued_count += 1
            self._idle_condition.notify_all()
            queue_size = len(self._queued_ids)
        return {
            "queued_count": queued_count,
            "queue_size": queue_size,
        }

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        with self._idle_condition:
            while self._queued_ids or self._active_ids:
                if deadline is None:
                    self._idle_condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle_condition.wait(remaining)
            return True

    def get_status(self) -> dict[str, int | bool]:
        with self._idle_condition:
            return {
                "workers_started": self._workers_started,
                "queued_count": len(self._queued_ids),
                "active_count": len(self._active_ids),
                "max_workers": max(1, int(self.config.max_workers)),
            }

    def shutdown(self, timeout: float | None = None) -> bool:
        with self._idle_condition:
            self._shutdown = True
            for _ in self._workers:
                self._queue.put(None)
            self._idle_condition.notify_all()
            workers = list(self._workers)
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        for worker in workers:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            worker.join(remaining)
        return all(not worker.is_alive() for worker in workers)

    def _start_workers_locked(self) -> None:
        worker_count = max(1, int(self.config.max_workers))
        for index in range(worker_count):
            worker = threading.Thread(
                target=self._worker_loop,
                name="source-probe-{index}".format(index=index),
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)
        self._workers_started = True

    def _worker_loop(self) -> None:
        while True:
            source_id = self._queue.get()
            if source_id is None:
                self._queue.task_done()
                return
            with self._idle_condition:
                self._queued_ids.discard(source_id)
                self._active_ids.add(source_id)
                self._idle_condition.notify_all()
            try:
                self._probe_source(source_id)
            finally:
                with self._idle_condition:
                    self._active_ids.discard(source_id)
                    self._idle_condition.notify_all()
                self._queue.task_done()

    def _probe_source(self, source_id: str) -> None:
        try:
            summary = self.registry.get_source_summary(source_id)
        except Exception as exc:
            self.health_store.record_failure(
                source_id,
                "search",
                error_code="missing_source",
                error_summary=str(exc),
            )
            self.health_store.mark_unknown(
                source_id,
                "preflight",
                summary="书源不存在，未执行目录预检",
            )
            self.health_store.mark_unknown(
                source_id,
                "download",
                summary="书源不存在，未执行下载探测",
            )
            return
        if self.source_profile_service is not None:
            try:
                self.source_profile_service.get(source_id, compile_if_missing=True)
            except Exception:
                pass

        issues_text = "；".join(summary.get("issues") or []) or "当前书源静态规则不支持探测"
        supports_search = bool(summary.get("supports_search", False))
        supports_download = bool(summary.get("supports_download", False))

        if not supports_search:
            self.health_store.mark_unsupported(source_id, "search", summary=issues_text)
            if supports_download:
                self.health_store.mark_unknown(
                    source_id,
                    "preflight",
                    summary="书源不支持按书名搜索，未自动预检",
                )
                self.health_store.mark_unknown(
                    source_id,
                    "download",
                    summary="书源不支持按书名搜索，未自动探测下载",
                )
            else:
                self.health_store.mark_unsupported(source_id, "preflight", summary=issues_text)
                self.health_store.mark_unsupported(source_id, "download", summary=issues_text)
            return

        if not supports_download:
            self.health_store.mark_unsupported(source_id, "preflight", summary=issues_text)
            self.health_store.mark_unsupported(source_id, "download", summary=issues_text)

        source = self.registry.load_normalized_source(source_id)
        keywords = tuple(self.config.probe_keywords) or SourceProbeServiceConfig.probe_keywords
        first_success_keyword = ""
        first_success_elapsed_ms = 0.0
        sample_keyword = ""
        sample_result: dict[str, Any] | None = None
        last_error_code = ""
        last_error_summary = ""
        last_error_elapsed_ms = 0.0

        for keyword in keywords:
            started_at = time.monotonic()
            try:
                results = self.engine.search_books(source, keyword, limit=3)
            except Exception as exc:
                last_error_code = self._classify_error_code(exc)
                last_error_summary = str(exc)
                last_error_elapsed_ms = max(0.0, (time.monotonic() - started_at) * 1000.0)
                continue

            elapsed_ms = max(0.0, (time.monotonic() - started_at) * 1000.0)
            if not first_success_keyword:
                first_success_keyword = keyword
                first_success_elapsed_ms = elapsed_ms
            for item in list(results or []):
                if str(item.get("book_url") or "").strip():
                    sample_keyword = keyword
                    sample_result = dict(item)
                    break
            if sample_result is not None:
                break

        if not first_success_keyword:
            self.health_store.record_failure(
                source_id,
                "search",
                elapsed_ms=last_error_elapsed_ms,
                error_code=last_error_code or "search_failed",
                error_summary=last_error_summary or "后台搜索探测失败",
                timeout=last_error_code == "timeout",
            )
            self.health_store.mark_unknown(
                source_id,
                "preflight",
                summary="搜索探测失败，未执行目录预检",
            )
            self.health_store.mark_unknown(
                source_id,
                "download",
                summary="搜索探测失败，未执行下载探测",
            )
            return

        search_summary = "搜索探测成功"
        if sample_result is None:
            search_summary = "搜索探测成功，但未命中可预检样本书"
        self.health_store.record_success(
            source_id,
            "search",
            elapsed_ms=first_success_elapsed_ms,
            summary=search_summary,
            metadata={
                "probe_keyword": first_success_keyword,
            },
        )
        if self.source_profile_service is not None:
            try:
                self.source_profile_service.update(
                    source_id,
                    {
                        "search_strategy": {
                            "last_probe_state": "healthy",
                            "last_probe_keyword": first_success_keyword,
                        }
                    },
                )
            except Exception:
                pass

        if not supports_download:
            return

        if sample_result is None:
            self.health_store.mark_unknown(
                source_id,
                "preflight",
                summary="探测关键词未命中可预检样本书",
            )
            self.health_store.mark_unknown(
                source_id,
                "download",
                summary="尚未进行正文下载探测",
            )
            return

        book_url = str(sample_result.get("book_url") or "").strip()
        book_name = str(sample_result.get("title") or "").strip()
        if not book_url:
            self.health_store.mark_unknown(
                source_id,
                "preflight",
                summary="探测样本缺少 book_url，未执行目录预检",
            )
            self.health_store.mark_unknown(
                source_id,
                "download",
                summary="尚未进行正文下载探测",
            )
            return

        started_at = time.monotonic()
        try:
            plan = self.engine.build_book_download_plan(source, book_url, book_name)
        except Exception as exc:
            self.health_store.record_failure(
                source_id,
                "preflight",
                elapsed_ms=max(0.0, (time.monotonic() - started_at) * 1000.0),
                error_code=self._classify_error_code(exc),
                error_summary=str(exc),
                timeout=self._classify_error_code(exc) == "timeout",
                metadata={
                    "sample_keyword": sample_keyword or first_success_keyword,
                    "sample_book_name": book_name,
                    "sample_book_url": book_url,
                },
            )
            self.health_store.mark_unknown(
                source_id,
                "download",
                summary="目录预检失败，未进入下载探测",
            )
            return

        self.health_store.record_success(
            source_id,
            "preflight",
            elapsed_ms=max(0.0, (time.monotonic() - started_at) * 1000.0),
            summary="目录预检成功",
            metadata={
                "sample_keyword": sample_keyword or first_success_keyword,
                "sample_book_name": str(plan.get("book_name") or book_name),
                "sample_book_url": str(plan.get("book_url") or book_url),
                "toc_count": len(plan.get("toc") or []),
            },
        )
        if self.source_profile_service is not None:
            try:
                self.source_profile_service.update(
                    source_id,
                    {
                        "download_strategy": {
                            "last_preflight_state": "healthy",
                            "last_toc_count": len(plan.get("toc") or []),
                        }
                    },
                )
            except Exception:
                pass
        self.health_store.mark_unknown(
            source_id,
            "download",
            summary="尚未进行正文下载探测",
        )

    def _classify_error_code(self, exc: Exception) -> str:
        text = str(exc or "").lower()
        if "timeout" in text or "timed out" in text or "超时" in text:
            return "timeout"
        if "network" in text or "网络" in text:
            return "network"
        if "目录" in text or "ruletoc" in text:
            return "preflight_rule_toc"
        if "http " in text:
            return "http"
        return exc.__class__.__name__.lower() or "error"
