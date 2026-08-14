#!/usr/bin/env python3
"""Opt-in protocol checks for the public Arsenal GitHub Releases repository."""

from __future__ import annotations

import json
import os
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


CATALOG_URL = os.environ.get("ARSENAL_REPOSITORY_CATALOG_URL", "")


@unittest.skipUnless(CATALOG_URL, "ARSENAL_REPOSITORY_CATALOG_URL is not set")
class RemoteRepositoryTests(unittest.TestCase):
    def request(self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None):
        request = Request(url, method=method, headers=headers or {})
        for attempt in range(3):
            try:
                return urlopen(request, timeout=30)
            except HTTPError:
                raise
            except (TimeoutError, URLError):
                if attempt == 2:
                    raise
                time.sleep(1 << attempt)
        raise AssertionError("unreachable retry state")

    def test_catalog(self) -> None:
        with self.request(CATALOG_URL) as response:
            self.assertEqual(response.status, 200)
            catalog = json.load(response)
        self.assertEqual(catalog["SchemaVersion"], 1)
        self.assertEqual(catalog["SourceId"], "arsenal")

    def assert_resumable(self, url: str) -> None:
        with self.request(url, method="HEAD") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Accept-Ranges"), "bytes")
            etag = response.headers["ETag"]
            size = int(response.headers["Content-Length"])
        self.assertGreater(size, 0)
        with self.request(url, headers={"Range": "bytes=0-0", "If-Match": etag}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers.get("Content-Range"), f"bytes 0-0/{size}")
            self.assertEqual(len(response.read()), 1)
        with self.assertRaises(HTTPError) as context:
            self.request(url, headers={"Range": "bytes=0-0", "If-Match": '"stale"'})
        self.assertEqual(context.exception.code, 412)

    def test_catalog_asset_supports_resumption(self) -> None:
        self.assert_resumable(CATALOG_URL)

    def test_first_package_supports_resumption(self) -> None:
        with self.request(CATALOG_URL) as response:
            catalog = json.load(response)
        if not catalog["Items"]:
            self.skipTest("repository has no packages yet")
        first_item = catalog["Items"][0]
        package_url = first_item.get("DownloadUrl")
        if not package_url:
            package_url = first_item["Files"][0]["DownloadUrl"]
        self.assertTrue(package_url.startswith("https://"))
        self.assert_resumable(package_url)

    def test_private_repository_state_is_not_a_release_asset(self) -> None:
        repository_root = CATALOG_URL.split("/releases/", 1)[0]
        with self.assertRaises(HTTPError) as context:
            self.request(repository_root + "/releases/latest/download/repository.json")
        self.assertEqual(context.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
