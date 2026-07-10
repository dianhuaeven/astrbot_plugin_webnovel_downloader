from __future__ import annotations

import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from astrbot_plugin_webnovel_downloader.core.session_scraper import (
    SessionScraper,
    SessionScraperConfig,
)
from astrbot_plugin_webnovel_downloader.core.url_security import (
    UnsafeUrlError,
    UrlSafetyPolicy,
)
from astrbot_plugin_webnovel_downloader.http_utils import ResponseTooLargeError


# 测试服务器跑在 127.0.0.1 上，默认策略会拒绝内网地址；本地服务器测试显式放行。
_ALLOW_LOCAL = UrlSafetyPolicy(allow_unsafe_urls=True)


class SessionScraperTest(unittest.TestCase):
    def _start_server(self, handler_factory):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 1)
        self.addCleanup(server.shutdown)
        return server

    def test_request_sets_default_user_agent_origin_and_referer(self):
        records = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                records["user_agent"] = self.headers.get("User-Agent")
                records["origin"] = self.headers.get("Origin")
                records["referer"] = self.headers.get("Referer")
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        server = self._start_server(Handler)
        scraper = SessionScraper(
            SessionScraperConfig(
                user_agent="Phase1TestAgent/1.0",
                max_retries=0,
                per_host_limit=2,
                url_safety_policy=_ALLOW_LOCAL,
            )
        )
        url = "http://127.0.0.1:{port}/book/1".format(port=server.server_address[1])

        response = scraper.request(url)

        self.assertEqual(response.body, b"ok")
        self.assertEqual(records["user_agent"], "Phase1TestAgent/1.0")
        self.assertEqual(
            records["origin"],
            "http://127.0.0.1:{port}".format(port=server.server_address[1]),
        )
        self.assertEqual(
            records["referer"],
            "http://127.0.0.1:{port}".format(port=server.server_address[1]),
        )

    def test_request_retries_transient_http_failure(self):
        state = {"count": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                state["count"] += 1
                if state["count"] == 1:
                    self.send_response(503)
                    self.end_headers()
                    return
                body = b"recovered"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        server = self._start_server(Handler)
        scraper = SessionScraper(
            SessionScraperConfig(
                user_agent="Phase1TestAgent/1.0",
                max_retries=1,
                retry_backoff=0.0,
                per_host_limit=2,
                url_safety_policy=_ALLOW_LOCAL,
            )
        )
        url = "http://127.0.0.1:{port}/retry".format(port=server.server_address[1])

        response = scraper.request(url, timeout=2.0)

        self.assertEqual(response.body, b"recovered")
        self.assertEqual(state["count"], 2)

    def test_request_honors_per_host_limit(self):
        state = {
            "active": 0,
            "peak": 0,
        }
        state_lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                with state_lock:
                    state["active"] += 1
                    state["peak"] = max(state["peak"], state["active"])
                time.sleep(0.25)
                body = b"limited"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                with state_lock:
                    state["active"] -= 1

            def log_message(self, format, *args):
                return

        server = self._start_server(Handler)
        scraper = SessionScraper(
            SessionScraperConfig(
                user_agent="Phase1TestAgent/1.0",
                max_retries=0,
                per_host_limit=1,
                url_safety_policy=_ALLOW_LOCAL,
            )
        )
        url = "http://127.0.0.1:{port}/limited".format(port=server.server_address[1])
        results: list[bytes] = []

        def _fetch():
            results.append(scraper.request(url, timeout=2.0).body)

        first = threading.Thread(target=_fetch)
        second = threading.Thread(target=_fetch)
        first.start()
        second.start()
        first.join()
        second.join()

        self.assertEqual(results, [b"limited", b"limited"])
        self.assertEqual(state["peak"], 1)

    def test_request_rejects_internal_address_by_default(self):
        # 默认策略（未放行 unsafe）下，规则构造的内网/本机 URL 必须在发请求前被拒。
        scraper = SessionScraper(
            SessionScraperConfig(
                user_agent="Phase1TestAgent/1.0",
                max_retries=0,
                per_host_limit=2,
            )
        )
        for url in (
            "http://127.0.0.1/secret",
            "http://localhost/secret",
            "http://192.168.0.1/secret",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeUrlError):
                    scraper.request(url, timeout=2.0)

    def test_request_blocks_redirect_into_internal_address(self):
        # 初始目标是回环测试服务器（被校验器放行），但它 302 跳到内网元数据地址；
        # 逐跳校验必须在“跟随重定向之前”拦下这一跳，而不是放行后才发现。
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data")
                self.end_headers()

            def log_message(self, format, *args):
                return

        server = self._start_server(Handler)
        port = server.server_address[1]

        from astrbot_plugin_webnovel_downloader import http_utils
        from urllib.request import Request

        blocked: list[str] = []

        def validator(url: str) -> None:
            # 放行回环测试服务器作为初始跳，只拦截典型云元数据内网地址。
            if "169.254.169.254" in url:
                blocked.append(url)
                raise UnsafeUrlError("blocked redirect target: " + url)

        request = Request("http://127.0.0.1:{port}/start".format(port=port))
        with self.assertRaises(UnsafeUrlError):
            http_utils.open_url(request, 2.0, redirect_validator=validator)
        self.assertTrue(blocked, "重定向目标未触发校验器")

    def test_response_limit_rejects_oversized_initial_response_in_both_backends(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "128")
                self.end_headers()

            def log_message(self, format, *args):
                return

        server = self._start_server(Handler)
        url = "http://127.0.0.1:{port}/large".format(port=server.server_address[1])
        self._assert_response_too_large_for_available_backends(url, 32)

    def test_response_limit_rejects_oversized_redirect_target_in_both_backends(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/large")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"x" * 128)

            def log_message(self, format, *args):
                return

        server = self._start_server(Handler)
        url = "http://127.0.0.1:{port}/start".format(port=server.server_address[1])
        self._assert_response_too_large_for_available_backends(url, 32)

    def _assert_response_too_large_for_available_backends(
        self, url: str, limit: int
    ) -> None:
        from astrbot_plugin_webnovel_downloader import http_utils

        original_httpx = http_utils.httpx
        backends = ["urllib"]
        if original_httpx is not None:
            backends.insert(0, "httpx")
        try:
            for backend in backends:
                with self.subTest(backend=backend):
                    http_utils.httpx = original_httpx if backend == "httpx" else None
                    scraper = SessionScraper(
                        SessionScraperConfig(
                            user_agent="ResponseLimitTest/1.0",
                            max_retries=0,
                            max_response_bytes=limit,
                            url_safety_policy=_ALLOW_LOCAL,
                        )
                    )
                    with self.assertRaises(ResponseTooLargeError):
                        scraper.request(url, timeout=2.0)
        finally:
            http_utils.httpx = original_httpx


if __name__ == "__main__":
    unittest.main()
