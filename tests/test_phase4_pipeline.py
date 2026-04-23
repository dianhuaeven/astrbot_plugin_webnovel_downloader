from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_webnovel_downloader.core.book_resolution_service import (
    BookResolutionService,
)
from astrbot_plugin_webnovel_downloader.core.download_orchestrator import (
    DownloadOrchestrator,
)
from astrbot_plugin_webnovel_downloader.core.search_service import (
    SearchService,
    SearchServiceConfig,
)
from astrbot_plugin_webnovel_downloader.core.source_health_store import (
    SourceHealthStore,
)


class _FakeProfileService(object):
    def __init__(self, profiles=None):
        self.profiles = {
            str(source_id): dict(profile)
            for source_id, profile in dict(profiles or {}).items()
        }
        self.updates = []

    def get(self, source_id, compile_if_missing=False):
        del compile_if_missing
        profile = self.profiles.get(str(source_id))
        if profile is None:
            return None
        return dict(profile)

    def update(self, source_id, patch):
        source_id = str(source_id)
        current = dict(self.profiles.get(source_id) or {"source_id": source_id})
        for key, value in dict(patch or {}).items():
            if isinstance(value, dict) and isinstance(current.get(key), dict):
                merged = dict(current[key])
                merged.update(value)
                current[key] = merged
            else:
                current[key] = value
        self.profiles[source_id] = current
        self.updates.append((source_id, dict(patch or {})))
        return dict(current)


class _SearchRegistry(object):
    def __init__(self, summaries, sources):
        self._summaries = {
            str(source_id): dict(summary)
            for source_id, summary in dict(summaries or {}).items()
        }
        self._sources = {
            str(source_id): dict(source)
            for source_id, source in dict(sources or {}).items()
        }

    def load_enabled_source_summaries(self, source_ids=None, include_disabled=False):
        selected = set(str(item) for item in list(source_ids or []))
        result = []
        for source_id, summary in self._summaries.items():
            if selected and source_id not in selected:
                continue
            if not include_disabled and not summary.get("enabled", True):
                continue
            result.append(dict(summary))
        return result

    def load_enabled_sources(self, source_ids=None, include_disabled=False):
        selected = set(str(item) for item in list(source_ids or []))
        result = []
        for source_id, source in self._sources.items():
            summary = self._summaries.get(source_id, {})
            if selected and source_id not in selected:
                continue
            if not include_disabled and not summary.get("enabled", True):
                continue
            result.append(dict(source))
        return result

    def get_source_summary(self, source_id):
        return dict(self._summaries[str(source_id)])


class _FakeSearchEngine(object):
    def __init__(self, results):
        self.results = {
            str(source_id): [dict(item) for item in list(items or [])]
            for source_id, items in dict(results or {}).items()
        }
        self.calls = []

    def search_books(self, source, keyword, limit):
        self.calls.append((str(source["source_id"]), keyword, limit))
        return list(self.results.get(str(source["source_id"]), []))


class _ResolutionRegistry(object):
    def __init__(self, summaries):
        self._summaries = {
            str(source_id): dict(summary)
            for source_id, summary in dict(summaries or {}).items()
        }

    def get_source_summary(self, source_id):
        return dict(self._summaries[str(source_id)])


class _FakeSearchService(object):
    def __init__(self, results):
        self.results = [dict(item) for item in list(results or [])]

    def search(self, keyword, source_ids=None, limit=20, include_disabled=False):
        del source_ids, limit, include_disabled
        return {
            "keyword": keyword,
            "searched_sources": len({item.get("source_id") for item in self.results}),
            "successful_sources": len({item.get("source_id") for item in self.results}),
            "result_count": len(self.results),
            "results": [dict(item) for item in self.results],
            "errors": [],
        }


class _FakeResolutionService(object):
    def __init__(self, candidates):
        self.candidates = [dict(item) for item in list(candidates or [])]

    def resolve(
        self, keyword, author="", source_ids=None, limit=20, include_disabled=False
    ):
        del keyword, author, source_ids, limit, include_disabled
        return {
            "keyword": "测试书",
            "author": "",
            "limit": 20,
            "candidate_count": len(self.candidates),
            "skipped_candidate_count": 0,
            "search_result": {
                "candidate_sources": len(self.candidates),
                "searched_sources": len(self.candidates),
                "successful_sources": len(self.candidates),
                "result_count": len(self.candidates),
            },
            "candidates": [dict(item) for item in self.candidates],
            "skipped_candidates": [],
        }


class _FakeSourceDownloadService(object):
    def __init__(self):
        self.preflight_calls = []
        self.sample_calls = []
        self.create_job_calls = []
        self.preflight_errors = {}
        self.sample_errors = {}
        self.job_errors = {}

    def preflight_book(self, source_id, book_url, book_name=""):
        self.preflight_calls.append((source_id, book_url, book_name))
        error = self.preflight_errors.get((source_id, book_url))
        if error is not None:
            raise error
        return {
            "source_id": source_id,
            "source_name": source_id,
            "book_url": book_url,
            "toc_url": book_url + "#toc",
            "book_name": book_name or "测试书",
            "author": "测试作者",
            "toc": [{"index": 0, "title": "第一章", "url": book_url + "/1"}],
            "toc_count": 1,
        }

    def sample_book(self, plan, chapter_count=None, min_content_chars=None):
        del chapter_count, min_content_chars
        source_id = str(plan.get("source_id") or "")
        book_url = str(plan.get("book_url") or "")
        self.sample_calls.append((source_id, book_url))
        error = self.sample_errors.get((source_id, book_url))
        if error is not None:
            raise error
        return {
            "sampled_chapter_count": 1,
            "sampled_chapters": [
                {
                    "index": 0,
                    "title": "第一章",
                    "url": book_url + "/1",
                    "content_chars": 128,
                    "elapsed_ms": 5.0,
                }
            ],
            "sample_errors": [],
        }

    def create_job_from_plan(self, plan, output_filename=""):
        self.create_job_calls.append((dict(plan), output_filename))
        source_id = str(plan.get("source_id") or "")
        book_url = str(plan.get("book_url") or "")
        error = self.job_errors.get((source_id, book_url))
        if error is not None:
            raise error
        return {
            "job_id": "job-" + source_id,
            "source_id": source_id,
            "source_name": plan.get("source_name", source_id),
            "book_name": plan.get("book_name", ""),
            "book_url": book_url,
            "toc_count": int(plan.get("toc_count", 0) or 0),
        }


class SearchServicePhase4Test(unittest.TestCase):
    def test_search_prefers_template_profile_sources_when_health_is_equal(self):
        registry = _SearchRegistry(
            {
                "template-source": {
                    "source_id": "template-source",
                    "name": "模板源",
                    "enabled": True,
                    "supports_search": True,
                    "supports_download": True,
                    "issues": [],
                },
                "fallback-source": {
                    "source_id": "fallback-source",
                    "name": "兜底源",
                    "enabled": True,
                    "supports_search": True,
                    "supports_download": True,
                    "issues": [],
                },
            },
            {
                "template-source": {"source_id": "template-source", "name": "模板源"},
                "fallback-source": {"source_id": "fallback-source", "name": "兜底源"},
            },
        )
        engine = _FakeSearchEngine(
            {
                "template-source": [
                    {"source_id": "template-source", "title": "测试书"}
                ],
                "fallback-source": [
                    {"source_id": "fallback-source", "title": "测试书"}
                ],
            }
        )
        profile_service = _FakeProfileService(
            {
                "template-source": {
                    "preferred_extractors": ["template_novelfull_like"]
                },
                "fallback-source": {"preferred_extractors": ["fallback_rule"]},
            }
        )
        service = SearchService(
            registry,
            engine,
            SearchServiceConfig(max_workers=1, time_budget_seconds=5.0),
            source_profile_service=profile_service,
        )

        payload = service.search("测试书", limit=5)

        self.assertEqual(engine.calls[0][0], "template-source")
        self.assertEqual(payload["results"][0]["source_id"], "template-source")


class BookResolutionPhase4Test(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)
        self.health_store = SourceHealthStore(self.base_dir / "source_health.json")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_resolver_prefers_template_backed_candidate_when_health_matches(self):
        registry = _ResolutionRegistry(
            {
                "template-source": {
                    "source_id": "template-source",
                    "name": "模板源",
                    "supports_download": True,
                    "issues": [],
                },
                "fallback-source": {
                    "source_id": "fallback-source",
                    "name": "兜底源",
                    "supports_download": True,
                    "issues": [],
                },
            }
        )
        search_service = _FakeSearchService(
            [
                {
                    "source_id": "fallback-source",
                    "source_name": "兜底源",
                    "title": "测试书",
                    "author": "测试作者",
                    "book_url": "https://example.com/fallback",
                },
                {
                    "source_id": "template-source",
                    "source_name": "模板源",
                    "title": "测试书",
                    "author": "测试作者",
                    "book_url": "https://example.com/template",
                },
            ]
        )
        profile_service = _FakeProfileService(
            {
                "template-source": {
                    "preferred_extractors": [
                        "template_novelfull_like",
                        "fallback_rule",
                    ],
                    "template_family": "novelfull_like",
                },
                "fallback-source": {
                    "preferred_extractors": ["fallback_rule"],
                    "template_family": "generic_html",
                },
            }
        )
        resolver = BookResolutionService(
            registry,
            search_service,
            self.health_store,
            source_profile_service=profile_service,
        )

        payload = resolver.resolve("测试书", author="测试作者", limit=10)

        self.assertEqual(payload["candidates"][0]["source_id"], "template-source")
        self.assertEqual(
            payload["candidates"][0]["preferred_extractor"], "template_novelfull_like"
        )


class DownloadOrchestratorPhase4Test(unittest.TestCase):
    def test_orchestrator_default_budget_reaches_success_beyond_first_five_candidates(
        self,
    ):
        candidates = []
        for index in range(6):
            source_id = (
                "late-good" if index == 5 else "broken-{idx}".format(idx=index + 1)
            )
            book_url = "https://example.com/{source}".format(source=source_id)
            candidates.append(
                {
                    "source_id": source_id,
                    "source_name": source_id,
                    "title": "测试书",
                    "author": "测试作者",
                    "book_url": book_url,
                }
            )
        resolver = _FakeResolutionService(candidates)
        downloader = _FakeSourceDownloadService()
        for index in range(5):
            source_id = "broken-{idx}".format(idx=index + 1)
            downloader.preflight_errors[
                (source_id, "https://example.com/{source}".format(source=source_id))
            ] = RuntimeError("目录页失败")
        orchestrator = DownloadOrchestrator(resolver, downloader)

        payload = orchestrator.auto_download("测试书", output_filename="测试书.txt")

        self.assertEqual(payload["status"], "started")
        self.assertEqual(payload["attempt_limit"], 12)
        self.assertEqual(payload["attempted_count"], 6)
        self.assertEqual(payload["attempts"][-1]["source_id"], "late-good")
        self.assertEqual(payload["selected"]["source_id"], "late-good")

    def test_orchestrator_falls_back_after_sample_failure(self):
        resolver = _FakeResolutionService(
            [
                {
                    "source_id": "broken-sample",
                    "source_name": "抽样失败源",
                    "title": "测试书",
                    "author": "测试作者",
                    "book_url": "https://example.com/broken",
                },
                {
                    "source_id": "good-source",
                    "source_name": "成功源",
                    "title": "测试书",
                    "author": "测试作者",
                    "book_url": "https://example.com/good",
                },
            ]
        )
        downloader = _FakeSourceDownloadService()
        downloader.sample_errors[("broken-sample", "https://example.com/broken")] = (
            RuntimeError("正文抽样失败")
        )
        profile_service = _FakeProfileService()
        orchestrator = DownloadOrchestrator(
            resolver,
            downloader,
            source_profile_service=profile_service,
        )

        payload = orchestrator.auto_download("测试书", output_filename="测试书.txt")

        self.assertEqual(payload["status"], "started")
        self.assertEqual(payload["attempts"][0]["outcome"], "sample_failed")
        self.assertEqual(payload["attempts"][1]["outcome"], "started")
        self.assertEqual(payload["selected"]["source_id"], "good-source")
        self.assertEqual(
            profile_service.profiles["broken-sample"]["download_strategy"][
                "last_sample_state"
            ],
            "failed",
        )
        self.assertEqual(
            profile_service.profiles["good-source"]["download_strategy"][
                "last_sample_state"
            ],
            "healthy",
        )

    def test_orchestrator_continues_after_job_create_failure(self):
        resolver = _FakeResolutionService(
            [
                {
                    "source_id": "job-broken",
                    "source_name": "建任务失败源",
                    "title": "测试书",
                    "author": "测试作者",
                    "book_url": "https://example.com/job-broken",
                },
                {
                    "source_id": "job-good",
                    "source_name": "建任务成功源",
                    "title": "测试书",
                    "author": "测试作者",
                    "book_url": "https://example.com/job-good",
                },
            ]
        )
        downloader = _FakeSourceDownloadService()
        downloader.job_errors[("job-broken", "https://example.com/job-broken")] = (
            RuntimeError("创建任务失败")
        )
        orchestrator = DownloadOrchestrator(resolver, downloader)

        payload = orchestrator.auto_download("测试书", output_filename="测试书.txt")

        self.assertEqual(payload["status"], "started")
        self.assertEqual(payload["attempts"][0]["outcome"], "job_create_failed")
        self.assertEqual(payload["attempts"][1]["outcome"], "started")
        self.assertEqual(payload["job"]["source_id"], "job-good")


if __name__ == "__main__":
    unittest.main()
