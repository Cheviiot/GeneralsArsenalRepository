#!/usr/bin/env python3
"""Opt-in protocol checks for the public Arsenal GitHub Releases repository."""

from __future__ import annotations

import json
import os
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


CATALOG_URL = os.environ.get("ARSENAL_REPOSITORY_CATALOG_URL", "")


@unittest.skipUnless(CATALOG_URL, "ARSENAL_REPOSITORY_CATALOG_URL is not set")
class RemoteRepositoryTests(unittest.TestCase):
    def request(self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None):
        return urlopen(Request(url, method=method, headers=headers or {}), timeout=30)

    def test_catalog(self) -> None:
        with self.request(CATALOG_URL) as response:
            self.assertEqual(response.status, 200)
            catalog = json.load(response)
        self.assertEqual(catalog["SchemaVersion"], 1)
        self.assertEqual(catalog["SourceId"], "arsenal")

    def test_first_package_supports_resumption(self) -> None:
        with self.request(CATALOG_URL) as response:
            catalog = json.load(response)
        if not catalog["Items"]:
            self.skipTest("repository has no packages yet")
        package_url = catalog["Items"][0]["DownloadUrl"]
        self.assertRegex(
            package_url,
            r"^https://github\.com/[^/]+/[^/]+/releases/download/objects-[0-9a-f]{2}/[0-9a-f]{64}\.[a-z0-9]+$",
        )
        with self.request(package_url, method="HEAD") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Accept-Ranges"), "bytes")
            etag = response.headers["ETag"]
            size = int(response.headers["Content-Length"])
        self.assertGreater(size, 0)
        with self.request(package_url, headers={"Range": "bytes=0-0", "If-Range": etag}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers.get("Content-Range"), f"bytes 0-0/{size}")
            self.assertEqual(len(response.read()), 1)
        with self.request(package_url, headers={"Range": "bytes=0-0", "If-Range": '"stale"'}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(int(response.headers["Content-Length"]), size)

    def test_private_repository_state_is_not_a_release_asset(self) -> None:
        repository_root = CATALOG_URL.split("/releases/", 1)[0]
        with self.assertRaises(HTTPError) as context:
            self.request(repository_root + "/releases/latest/download/repository.json")
        self.assertEqual(context.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
