from __future__ import annotations

import base64
import hashlib
import time
import unittest

from astrbot_plugin_webnovel_downloader.core.js_runtime import (
    JavaScriptRuntime,
    JavaScriptRuntimeConfig,
    JavaScriptTimeoutError,
    quickjs,
)


@unittest.skipIf(
    quickjs is None,
    "quickjs dependency is required to exercise JS rule support",
)
class JavaScriptRuntimeTest(unittest.TestCase):
    def _runtime(self, timeout_seconds: float = 2.0) -> JavaScriptRuntime:
        return JavaScriptRuntime(
            JavaScriptRuntimeConfig(timeout_seconds=timeout_seconds)
        )

    def test_hard_timeout_interrupts_infinite_loop(self):
        # 核心保障：死循环 JS 必须被原生墙钟超时打断，而不是永久泄漏工作线程。
        runtime = self._runtime(timeout_seconds=0.3)
        start = time.perf_counter()
        with self.assertRaises(JavaScriptTimeoutError):
            runtime.evaluate("while(true){}")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 2.0, "死循环未在超时窗口内被中断")

    def test_fast_rule_completes_under_time_limit(self):
        runtime = self._runtime()
        self.assertEqual(runtime.evaluate("1 + 1"), 2)

    def test_md5_matches_python_including_cjk(self):
        runtime = self._runtime()
        for text in ("abc", "Hello, World!", "诡秘之主"):
            with self.subTest(text=text):
                self.assertEqual(
                    runtime.evaluate(
                        "java.md5Encode({text})".format(text=_js_string(text))
                    ),
                    hashlib.md5(text.encode("utf-8")).hexdigest(),
                )

    def test_base64_encode_matches_python_and_roundtrips(self):
        runtime = self._runtime()
        for text in ("abc", "Hello, 世界!", "雪中悍刀行123"):
            with self.subTest(text=text):
                encoded = runtime.evaluate(
                    "java.base64Encode({text})".format(text=_js_string(text))
                )
                self.assertEqual(
                    encoded,
                    base64.b64encode(text.encode("utf-8")).decode("ascii"),
                )
                decoded = runtime.evaluate(
                    "java.base64Decode({enc})".format(enc=_js_string(encoded))
                )
                self.assertEqual(decoded, text)

    def test_context_mutation_is_written_back(self):
        # java.put 写入的值必须经返回值信封带回 Python 的 rule_context。
        runtime = self._runtime()
        rule_context = {"key": "雪中"}
        result = runtime.evaluate(
            "java.put('x', java.get('key') + '!'); java.get('x')",
            rule_context=rule_context,
        )
        self.assertEqual(result, "雪中!")
        self.assertEqual(rule_context.get("x"), "雪中!")

    def test_encode_uri_uses_native_component_encoding(self):
        runtime = self._runtime()
        self.assertEqual(runtime.evaluate("java.encodeURI('a b&c')"), "a%20b%26c")

    def test_object_return_value_is_deserialized(self):
        runtime = self._runtime()
        self.assertEqual(
            runtime.evaluate("var o = {}; o.a = 1; o.b = 2; o", payload_kind="json"),
            {"a": 1, "b": 2},
        )

    def test_get_string_is_unsupported(self):
        # java.getString 依赖父进程实时文档，无回调宿主下不再支持，须抛明确错误。
        runtime = self._runtime()
        with self.assertRaises(Exception) as captured:
            runtime.evaluate("java.getString('.title')")
        self.assertIn("不支持", str(captured.exception))

    def test_network_methods_remain_unsupported(self):
        runtime = self._runtime()
        for code in ("java.ajax('http://x')", "java.post('http://x')"):
            with self.subTest(code=code):
                with self.assertRaises(Exception):
                    runtime.evaluate(code)


def _js_string(value: str) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
