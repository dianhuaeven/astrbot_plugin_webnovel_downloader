from __future__ import annotations

import unittest

from astrbot_plugin_webnovel_downloader.core.url_security import (
    UnsafeUrlError,
    UrlSafetyPolicy,
    validate_user_fetch_url,
)


class UrlSecurityTest(unittest.TestCase):
    def test_rejects_file_url(self):
        with self.assertRaises(UnsafeUrlError):
            validate_user_fetch_url("file:///tmp/book.html")

    def test_rejects_loopback_and_private_ip(self):
        for url in (
            "http://127.0.0.1/book",
            "http://localhost/book",
            "http://192.168.1.10/book",
            "http://[::1]/book",
        ):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeUrlError):
                    validate_user_fetch_url(url)

    def test_accepts_public_http_without_dns_resolution(self):
        policy = UrlSafetyPolicy(resolve_hostnames=False)
        self.assertEqual(
            validate_user_fetch_url("https://example.com/book", policy=policy),
            "https://example.com/book",
        )

    def test_allow_unsafe_urls_bypasses_policy_for_tests_and_admin_diagnostics(self):
        policy = UrlSafetyPolicy(allow_unsafe_urls=True)
        self.assertEqual(
            validate_user_fetch_url("file:///tmp/book.html", policy=policy),
            "file:///tmp/book.html",
        )


if __name__ == "__main__":
    unittest.main()
