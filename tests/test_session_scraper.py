from __future__ import annotations

import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from astrbot_plugin_webnovel_downloader.core.session_scraper import (
    SessionScraper,
    SessionScraperConfig,
)


class SessionScraperTest(unittest.TestCase):
    def _start_server(self, handler_factory):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 1)
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


if __name__ == "__main__":
    unittest.main()
