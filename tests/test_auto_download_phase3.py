from __future__ import annotations

import importlib
import inspect
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_webnovel_downloader.core.source_health_store import SourceHealthStore


class _FakeRegistry(object):
    def __init__(self, summaries):
        self._summaries = {
            str(source_id): dict(summary)
            for source_id, summary in (summaries or {}).items()
        }

    def get_source_summary(self, source_id):
        return dict(self._summaries[source_id])

    def load_enabled_source_summaries(self, source_ids=None, include_disabled=False):
        selected_ids = set(source_ids or [])
        result = []
        for source_id, summary in self._summaries.items():
            if selected_ids and source_id not in selected_ids:
                continue
            if not include_disabled and not summary.get("enabled", True):
                continue
            result.append(dict(summary))
        return result


class _FakeSearchService(object):
    def __init__(self, results):
        self._results = [dict(item) for item in (results or [])]
        self.calls = []

    def search(self, keyword, source_ids=None, limit=20, include_disabled=False):
        self.calls.append(
            {
                "keyword": keyword,
                "source_ids": list(source_ids or []),
                "limit": limit,
                "include_disabled": include_disabled,
            }
        )
        return {
            "keyword": keyword,
            "searched_sources": len({item.get("source_id", "") for item in self._results}),
            "successful_sources": len({item.get("source_id", "") for item in self._results}),
            "result_count": len(self._results),
            "results": [dict(item) for item in self._results],
            "errors": [],
        }


class _FakeResolutionService(object):
    def __init__(self, candidates):
        self._candidates = [dict(item) for item in (candidates or [])]
        self.calls = []

    def resolve_candidates(
        self,
        keyword,
        author="",
        limit=20,
        source_ids=None,
        include_disabled=False,
        **_,
    ):
        self.calls.append(
            {
                "keyword": keyword,
                "author": author,
                "limit": limit,
                "source_ids": list(source_ids or []),
                "include_disabled": include_disabled,
            }
        )
        return {
            "keyword": keyword,
            "author": author,
            "candidate_count": len(self._candidates),
            "candidates": [dict(item) for item in self._candidates],
        }

    def resolve(self, *args, **kwargs):
        keyword = args[0] if len(args) > 0 else kwargs.get("keyword", "")
        author = args[1] if len(args) > 1 else kwargs.get("author", "")
        source_ids = args[2] if len(args) > 2 else kwargs.get("source_ids")
        limit = args[3] if len(args) > 3 else kwargs.get("limit", 20)
        include_disabled = (
            args[4] if len(args) > 4 else kwargs.get("include_disabled", False)
        )
        return self.resolve_candidates(
            keyword,
            author=author,
            source_ids=source_ids,
            limit=limit,
            include_disabled=include_disabled,
        )


class _FakeSourceDownloadService(object):
    def __init__(self):
        self.preflight_calls = []
        self.sample_calls = []
        self.create_job_calls = []
        self._plans = {}
        self._errors = {}
        self._sample_errors = {}

    def add_success(self, source_id, book_url, plan=None):
        self._plans[(source_id, book_url)] = dict(plan or {})

    def add_failure(self, source_id, book_url, error):
        self._errors[(source_id, book_url)] = error

    def add_sample_failure(self, source_id, book_url, error):
        self._sample_errors[(source_id, book_url)] = error

    def preflight_book(self, source_id, book_url, book_name=""):
        self.preflight_calls.append(
            {
                "source_id": source_id,
                "book_url": book_url,
                "book_name": book_name,
            }
        )
        error = self._errors.get((source_id, book_url))
        if error is not None:
            raise error
        plan = dict(
            self._plans.get(
                (source_id, book_url),
                {
                    "source_id": source_id,
                    "source_name": source_id,
                    "book_url": book_url,
                    "toc_url": book_url + "#toc",
                    "book_name": book_name or "测试书",
                    "author": "",
                    "intro": "",
                    "toc": [{"index": 0, "title": "第一章", "url": book_url + "/1"}],
                    "toc_count": 1,
                },
            )
        )
        plan.setdefault("source_id", source_id)
        plan.setdefault("source_name", source_id)
        plan.setdefault("book_url", book_url)
        plan.setdefault("book_name", book_name or "测试书")
        plan.setdefault("toc_url", book_url + "#toc")
        plan.setdefault("toc", [{"index": 0, "title": "第一章", "url": book_url + "/1"}])
        plan.setdefault("toc_count", len(plan.get("toc") or []))
        return plan

    def sample_book(self, plan, chapter_count=None, min_content_chars=None):
        del chapter_count, min_content_chars
        source_id = str(plan.get("source_id") or "").strip()
        book_url = str(plan.get("book_url") or "").strip()
        self.sample_calls.append(
            {
                "source_id": source_id,
                "book_url": book_url,
            }
        )
        error = self._sample_errors.get((source_id, book_url))
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
        self.create_job_calls.append(
            {
                "plan": dict(plan),
                "output_filename": output_filename,
            }
        )
        source_id = str(plan.get("source_id") or "").strip()
        return {
            "job_id": "job-for-" + source_id,
            "source_id": source_id,
            "source_name": plan.get("source_name", source_id),
            "book_name": plan.get("book_name", ""),
            "book_url": plan.get("book_url", ""),
            "toc_url": plan.get("toc_url", ""),
            "toc_count": int(plan.get("toc_count", len(plan.get("toc") or [])) or 0),
            "preflight": {
                "source_id": source_id,
                "source_name": plan.get("source_name", source_id),
                "book_name": plan.get("book_name", ""),
                "book_url": plan.get("book_url", ""),
                "toc_url": plan.get("toc_url", ""),
                "toc_count": int(plan.get("toc_count", len(plan.get("toc") or [])) or 0),
            },
        }


class _Phase3ContractTestMixin(object):
    def _load_phase3_class(self, module_name, class_name):
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                self.skipTest("Phase 3 实现尚未落地: 缺少模块 {name}".format(name=module_name))
            raise
        phase3_class = getattr(module, class_name, None)
        if phase3_class is None:
            self.skipTest(
                "Phase 3 实现尚未落地: {module} 中缺少 {name}".format(
                    module=module_name,
                    name=class_name,
                )
            )
        return module, phase3_class

    def _build_instance(self, module, phase3_class, dependency_map):
        signature = inspect.signature(phase3_class.__init__)
        kwargs = {}
        for name, parameter in list(signature.parameters.items())[1:]:
            if name in dependency_map:
                kwargs[name] = dependency_map[name]
                continue
            if parameter.default is not inspect.Signature.empty:
                continue
            if name == "config":
                config = self._build_default_config(module, phase3_class.__name__)
                if config is not None:
                    kwargs[name] = config
                    continue
            self.fail(
                "无法为 {name}.__init__ 的必填参数 {param} 注入测试依赖".format(
                    name=phase3_class.__name__,
                    param=name,
                )
            )
        return phase3_class(**kwargs)

    def _build_default_config(self, module, class_name):
        preferred_names = (
            class_name.replace("Service", "Config"),
            class_name + "Config",
        )
        for name in preferred_names:
            config_class = getattr(module, name, None)
            if inspect.isclass(config_class):
                try:
                    return config_class()
                except TypeError:
                    continue
        for name, config_class in inspect.getmembers(module, inspect.isclass):
            if not name.endswith("Config"):
                continue
            try:
                return config_class()
            except TypeError:
                continue
        return None

    def _call_first_available(self, target, method_names, **kwargs):
        for method_name in method_names:
            method = getattr(target, method_name, None)
            if callable(method):
                return self._call_with_supported_kwargs(method, **kwargs)
        self.fail(
            "未找到可用方法: {names}".format(
                names=", ".join(method_names),
            )
        )

    def _call_with_supported_kwargs(self, method, **kwargs):
        signature = inspect.signature(method)
        supported_kwargs = {}
        for name in signature.parameters:
            if name == "self":
                continue
            if name in kwargs:
                supported_kwargs[name] = kwargs[name]
        return method(**supported_kwargs)

    def _extract_candidates(self, payload):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("candidates", "results", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        self.fail("候选解析结果结构无法识别: {payload!r}".format(payload=payload))

    def _extract_attempts(self, payload):
        if not isinstance(payload, dict):
            self.fail("下载编排结果结构无法识别: {payload!r}".format(payload=payload))
        attempts = payload.get("attempts")
        if isinstance(attempts, list):
            return attempts
        attempts = payload.get("preflight_attempts")
        if isinstance(attempts, list):
            return attempts
        self.fail("下载编排结果缺少 attempts/preflight_attempts: {payload!r}".format(payload=payload))

    def _extract_chosen(self, payload):
        if not isinstance(payload, dict):
            self.fail("下载编排结果结构无法识别: {payload!r}".format(payload=payload))
        chosen = payload.get("chosen")
        if isinstance(chosen, dict):
            return chosen
        chosen = payload.get("chosen_candidate")
        if isinstance(chosen, dict):
            return chosen
        chosen = payload.get("selected")
        if isinstance(chosen, dict):
            return chosen
        self.fail("下载编排结果缺少 chosen/chosen_candidate: {payload!r}".format(payload=payload))

    def _extract_job_info(self, payload):
        if not isinstance(payload, dict):
            self.fail("下载编排结果结构无法识别: {payload!r}".format(payload=payload))
        job_info = payload.get("job_info")
        if isinstance(job_info, dict):
            return job_info
        job_info = payload.get("job")
        if isinstance(job_info, dict):
            return job_info
        self.fail("下载编排结果缺少 job_info/job: {payload!r}".format(payload=payload))

    def _extract_source_id(self, payload):
        if isinstance(payload, dict):
            source_id = payload.get("source_id")
            if source_id:
                return source_id
            candidate = payload.get("candidate")
            if isinstance(candidate, dict) and candidate.get("source_id"):
                return candidate["source_id"]
        self.fail("结果项缺少 source_id: {payload!r}".format(payload=payload))

    def _extract_error_text(self, payload):
        if not isinstance(payload, dict):
            return ""
        for key in ("error", "error_summary", "reason", "message"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        return ""


class BookResolutionPhase3ContractTest(_Phase3ContractTestMixin, unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)
        self.health_store = SourceHealthStore(self.base_dir / "source_health.json")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_resolver_prefers_exact_title_and_healthier_downloadable_source(self):
        summaries = {
            "healthy-downloadable": {
                "source_id": "healthy-downloadable",
                "name": "健康可下载源",
                "enabled": True,
                "supports_search": True,
                "supports_download": True,
                "issues": [],
            },
            "degraded-downloadable": {
                "source_id": "degraded-downloadable",
                "name": "一般可下载源",
                "enabled": True,
                "supports_search": True,
                "supports_download": True,
                "issues": [],
            },
            "partial-downloadable": {
                "source_id": "partial-downloadable",
                "name": "偏题可下载源",
                "enabled": True,
                "supports_search": True,
                "supports_download": True,
                "issues": [],
            },
            "unsupported-static": {
                "source_id": "unsupported-static",
                "name": "静态不支持源",
                "enabled": True,
                "supports_search": True,
                "supports_download": False,
                "issues": ["ruleToc 含 JS 规则"],
            },
        }
        registry = _FakeRegistry(summaries)
        search_service = _FakeSearchService(
            [
                {
                    "source_id": "unsupported-static",
                    "source_name": "静态不支持源",
                    "title": "黎明医生",
                    "author": "机器人瓦力",
                    "book_url": "https://example.com/unsupported",
                },
                {
                    "source_id": "degraded-downloadable",
                    "source_name": "一般可下载源",
                    "title": "黎明医生",
                    "author": "机器人瓦力",
                    "book_url": "https://example.com/degraded",
                },
                {
                    "source_id": "partial-downloadable",
                    "source_name": "偏题可下载源",
                    "title": "黎明医生全文阅读",
                    "author": "机器人瓦力",
                    "book_url": "https://example.com/partial",
                },
                {
                    "source_id": "healthy-downloadable",
                    "source_name": "健康可下载源",
                    "title": "黎明医生",
                    "author": "机器人瓦力",
                    "book_url": "https://example.com/healthy",
                },
            ]
        )

        self.health_store.record_success(
            "healthy-downloadable",
            "search",
            elapsed_ms=80.0,
            summary="搜索稳定",
        )
        self.health_store.record_success(
            "healthy-downloadable",
            "preflight",
            elapsed_ms=90.0,
            summary="目录预检成功",
        )
        self.health_store.record_success(
            "degraded-downloadable",
            "search",
            elapsed_ms=180.0,
            summary="搜索成功",
        )
        self.health_store.record_success(
            "degraded-downloadable",
            "preflight",
            elapsed_ms=220.0,
            summary="曾经预检成功",
        )
        self.health_store.record_failure(
            "degraded-downloadable",
            "preflight",
            elapsed_ms=420.0,
            error_code="timeout",
            error_summary="最近一次目录页超时",
            timeout=True,
        )
        self.health_store.record_success(
            "partial-downloadable",
            "search",
            elapsed_ms=60.0,
            summary="搜索成功",
        )
        self.health_store.record_success(
            "partial-downloadable",
            "preflight",
            elapsed_ms=140.0,
            summary="目录预检成功",
        )

        module, resolution_class = self._load_phase3_class(
            "astrbot_plugin_webnovel_downloader.core.book_resolution_service",
            "BookResolutionService",
        )
        resolver = self._build_instance(
            module,
            resolution_class,
            {
                "search_service": search_service,
                "searcher": search_service,
                "source_registry": registry,
                "registry": registry,
                "source_health_store": self.health_store,
                "health_store": self.health_store,
            },
        )

        payload = self._call_first_available(
            resolver,
            ("resolve_candidates", "resolve", "rank_candidates"),
            keyword="黎明医生",
            author="机器人瓦力",
            limit=10,
            include_disabled=False,
        )
        candidates = self._extract_candidates(payload)
        source_ids = [self._extract_source_id(item) for item in candidates]

        self.assertTrue(search_service.calls, "解析服务应触发一次底层搜索")
        self.assertGreaterEqual(len(candidates), 3)
        self.assertEqual(source_ids[0], "healthy-downloadable")
        self.assertEqual(source_ids[1], "degraded-downloadable")
        self.assertNotIn("unsupported-static", source_ids)
        self.assertIn("partial-downloadable", source_ids)


class DownloadOrchestratorPhase3ContractTest(_Phase3ContractTestMixin, unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)
        self.health_store = SourceHealthStore(self.base_dir / "source_health.json")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_orchestrator_falls_back_after_preflight_failure_and_returns_choice(self):
        candidates = [
            {
                "source_id": "first-broken",
                "source_name": "首个失败源",
                "title": "黎明医生",
                "author": "机器人瓦力",
                "book_url": "https://example.com/first",
            },
            {
                "source_id": "second-good",
                "source_name": "第二个成功源",
                "title": "黎明医生",
                "author": "机器人瓦力",
                "book_url": "https://example.com/second",
            },
        ]
        resolution_service = _FakeResolutionService(candidates)
        download_service = _FakeSourceDownloadService()
        download_service.add_failure(
            "first-broken",
            "https://example.com/first",
            RuntimeError("目录页超时"),
        )
        download_service.add_success(
            "second-good",
            "https://example.com/second",
            {
                "source_id": "second-good",
                "source_name": "第二个成功源",
                "book_url": "https://example.com/second",
                "toc_url": "https://example.com/second#toc",
                "book_name": "黎明医生",
                "author": "机器人瓦力",
                "intro": "测试简介",
                "toc": [
                    {
                        "index": 0,
                        "title": "第一章",
                        "url": "https://example.com/second/1",
                    },
                    {
                        "index": 1,
                        "title": "第二章",
                        "url": "https://example.com/second/2",
                    },
                ],
                "toc_count": 2,
            },
        )

        module, orchestrator_class = self._load_phase3_class(
            "astrbot_plugin_webnovel_downloader.core.download_orchestrator",
            "DownloadOrchestrator",
        )
        orchestrator = self._build_instance(
            module,
            orchestrator_class,
            {
                "resolution_service": resolution_service,
                "resolver": resolution_service,
                "book_resolution_service": resolution_service,
                "source_download_service": download_service,
                "download_service": download_service,
                "downloader": download_service,
                "source_health_store": self.health_store,
                "health_store": self.health_store,
            },
        )

        payload = self._call_first_available(
            orchestrator,
            ("auto_download", "orchestrate_download", "download"),
            keyword="黎明医生",
            author="机器人瓦力",
            output_filename="黎明医生.txt",
        )
        attempts = self._extract_attempts(payload)
        chosen = self._extract_chosen(payload)
        job_info = self._extract_job_info(payload)

        self.assertEqual(len(download_service.preflight_calls), 2)
        self.assertEqual(download_service.preflight_calls[0]["source_id"], "first-broken")
        self.assertEqual(download_service.preflight_calls[1]["source_id"], "second-good")
        self.assertEqual(len(download_service.create_job_calls), 1)
        self.assertEqual(download_service.create_job_calls[0]["plan"]["source_id"], "second-good")

        self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual(self._extract_source_id(attempts[0]), "first-broken")
        self.assertIn("超时", self._extract_error_text(attempts[0]))
        self.assertEqual(self._extract_source_id(attempts[1]), "second-good")
        self.assertEqual(self._extract_source_id(chosen), "second-good")
        self.assertEqual(job_info["job_id"], "job-for-second-good")
        self.assertEqual(job_info["source_id"], "second-good")


if __name__ == "__main__":
    unittest.main()
