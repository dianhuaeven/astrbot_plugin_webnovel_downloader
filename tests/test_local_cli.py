from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from astrbot_plugin_webnovel_downloader import local_smoke


class LocalSmokeCliTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def _start_server(self):
        records: dict[str, str] = {"keyword": ""}
        payload_holder: dict[str, str] = {"sources": "[]"}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path == "/sources.json":
                    body = payload_holder["sources"].encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/search":
                    keyword = parse_qs(parsed.query).get("q", [""])[0]
                    records["keyword"] = keyword
                    body = json.dumps(
                        {
                            "data": {
                                "items": [
                                    {
                                        "title": "本地冒烟命中",
                                        "author": "测试作者",
                                        "url": "https://example.com/books/local-hit",
                                        "intro": keyword,
                                    }
                                ]
                            }
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 1)

        base_url = "http://127.0.0.1:{port}".format(port=server.server_address[1])
        payload_holder["sources"] = json.dumps(
            [
                {
                    "bookSourceName": "本地CLI测试源",
                    "bookSourceUrl": base_url,
                    "searchUrl": base_url + "/search?q={{key}}",
                    "ruleSearch": {
                        "bookList": "data.items",
                        "name": "title",
                        "author": "author",
                        "bookUrl": "url",
                        "intro": "intro",
                    },
                    "ruleBookInfo": {"name": "h1&&text"},
                    "ruleToc": {
                        "chapterList": "#toc a",
                        "chapterName": "text",
                        "chapterUrl": "@href",
                    },
                    "ruleContent": {"content": "#content&&text"},
                }
            ],
            ensure_ascii=False,
        )
        return base_url + "/sources.json", records

    def test_local_smoke_import_search_and_list_sources(self):
        sources_url, records = self._start_server()
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = local_smoke.main(
                [
                    "--data-dir",
                    str(self.base_dir / "runtime"),
                    "--source-json",
                    sources_url,
                    "--keyword",
                    "诡秘之主",
                    "--list-sources",
                ]
            )

        self.assertEqual(exit_code, 0, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["import"]["imported_count"], 1)
        self.assertEqual(payload["sources"]["total_count"], 1)
        self.assertEqual(payload["search"]["result_count"], 1)
        self.assertEqual(records["keyword"], "诡秘之主")
        self.assertTrue(Path(payload["registry_path"]).exists())

    def test_local_smoke_can_reuse_existing_registry(self):
        sources_url, records = self._start_server()
        data_dir = self.base_dir / "runtime"
        first_stdout = io.StringIO()
        first_stderr = io.StringIO()

        with (
            contextlib.redirect_stdout(first_stdout),
            contextlib.redirect_stderr(first_stderr),
        ):
            first_exit_code = local_smoke.main(
                [
                    "--data-dir",
                    str(data_dir),
                    "--source-json",
                    sources_url,
                ]
            )

        self.assertEqual(first_exit_code, 0, first_stderr.getvalue())
        second_stdout = io.StringIO()
        second_stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(second_stdout),
            contextlib.redirect_stderr(second_stderr),
        ):
            second_exit_code = local_smoke.main(
                [
                    "--data-dir",
                    str(data_dir),
                    "--keyword",
                    "雪中悍刀行",
                ]
            )

        self.assertEqual(second_exit_code, 0, second_stderr.getvalue())
        payload = json.loads(second_stdout.getvalue())
        self.assertEqual(payload["search"]["result_count"], 1)
        self.assertEqual(records["keyword"], "雪中悍刀行")


if __name__ == "__main__":
    unittest.main()
