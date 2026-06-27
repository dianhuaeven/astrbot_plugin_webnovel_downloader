from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from astrbot_plugin_webnovel_downloader.clean_rule_store import (
    CleanRuleRepositoryStore,
)
from astrbot_plugin_webnovel_downloader.core.source_registry import SourceRegistry
from astrbot_plugin_webnovel_downloader.search_cache import SearchCacheStore


def _run_concurrently(targets: list) -> list:
    # 用屏障让所有线程尽量同时进入「读-改-写」临界区，放大无锁时的丢更新概率。
    barrier = threading.Barrier(len(targets))
    errors: list[BaseException] = []

    def _wrap(fn):
        def _inner():
            try:
                barrier.wait()
                fn()
            except BaseException as exc:  # noqa: BLE001 - 收集供断言
                errors.append(exc)

        return _inner

    threads = [threading.Thread(target=_wrap(fn)) for fn in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


class StoreConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_concurrent_source_imports_keep_every_source(self):
        registry = SourceRegistry(self.base_dir)
        count = 12

        def _make_import(index: int):
            payload = (
                '[{{"bookSourceName":"并发源{index}",'
                '"bookSourceUrl":"https://example{index}.test"}}]'
            ).format(index=index)
            return lambda: registry.import_sources_from_text(payload)

        errors = _run_concurrently([_make_import(i) for i in range(count)])
        self.assertEqual(errors, [], "并发导入抛出异常")

        sources = registry.list_sources()
        # 每个源的 bookSourceUrl 不同 → source_id 不同；加锁后注册表应保留全部。
        self.assertEqual(len(sources), count)

    def test_concurrent_saves_keep_every_search_in_index(self):
        store = SearchCacheStore(self.base_dir)
        count = 16

        def _make_save(index: int):
            result = {
                "searched_sources": 1,
                "successful_sources": 1,
                "results": [{"title": "书{index}".format(index=index)}],
                "errors": [],
            }
            return lambda: store.save_search(
                "关键词{index}".format(index=index), result
            )

        errors = _run_concurrently([_make_save(i) for i in range(count)])
        self.assertEqual(errors, [], "并发保存搜索抛出异常")

        searches = store.list_searches()
        # 每条搜索 search_id 含关键词+摘要，互不相同；加锁后索引应保留全部。
        self.assertEqual(len(searches), count)

    def test_concurrent_clean_rule_imports_keep_every_repo(self):
        store = CleanRuleRepositoryStore(self.base_dir)
        count = 12

        def _make_import(index: int):
            payload = (
                '[{{"name":"规则{index}","pattern":"广告{index}",'
                '"replacement":"","isRegex":false}}]'
            ).format(index=index)
            return lambda: store.import_rules_from_text(
                payload,
                "仓库{index}".format(index=index),
                "https://example.com/{index}.json".format(index=index),
            )

        errors = _run_concurrently([_make_import(i) for i in range(count)])
        self.assertEqual(errors, [], "并发导入净化规则抛出异常")

        repositories = store.list_repositories()
        self.assertEqual(len(repositories), count)


if __name__ == "__main__":
    unittest.main()
