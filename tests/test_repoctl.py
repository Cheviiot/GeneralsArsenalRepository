#!/usr/bin/env python3
"""Tests for the dependency-free Arsenal repository publisher."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPOCTL = Path(__file__).resolve().parents[1] / "repoctl.py"


class RepositoryToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="arsenal-repository-test-")
        self.root = Path(self.temporary.name) / "data"
        self.package = Path(self.temporary.name) / "example.zip"
        with zipfile.ZipFile(self.package, "w") as archive:
            archive.writestr("Data/INI/example.ini", "value=yes\n")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_tool(self, *arguments: str, success: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(REPOCTL), "--root", str(self.root), *arguments],
            check=False,
            text=True,
            capture_output=True,
        )
        if success and result.returncode != 0:
            self.fail(f"repoctl failed: {result.stderr}")
        if not success and result.returncode == 0:
            self.fail("repoctl unexpectedly succeeded")
        return result

    def initialize(self) -> None:
        self.run_tool("init", "--github-repository", "Example/ArsenalRepository")

    def test_publish_and_verify_content_addressed_package(self) -> None:
        self.initialize()
        self.run_tool(
            "add", "--engine", "generals", "--type", "mod", "--id", "example-mod",
            "--name", "Example Mod", "--version", "1.0", "--package", str(self.package),
            "--moddb-url", "https://www.moddb.com/mods/example",
        )
        catalog = json.loads((self.root / "public/v1/catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog["SourceId"], "arsenal")
        self.assertEqual(catalog["Items"][0]["Id"], "example-mod")
        self.assertRegex(
            catalog["Items"][0]["DownloadUrl"],
            r"^https://github\.com/Example/ArsenalRepository/releases/download/objects-[0-9a-f]{2}/[0-9a-f]{64}\.zip$",
        )
        self.run_tool("verify")

    def test_tamper_is_rejected(self) -> None:
        self.initialize()
        self.run_tool(
            "add", "--engine", "zerohour", "--type", "mod", "--id", "example",
            "--name", "Example", "--version", "2", "--package", str(self.package),
        )
        package = next((self.root / "public/v1/packages").rglob("*.zip"))
        package.write_bytes(package.read_bytes() + b"tampered")
        result = self.run_tool("verify", success=False)
        self.assertIn("integrity mismatch", result.stderr)

    def test_github_repository_changes_without_republishing_objects(self) -> None:
        self.initialize()
        self.run_tool(
            "add", "--engine", "generals", "--type", "mod", "--id", "example",
            "--name", "Example", "--version", "1", "--package", str(self.package),
        )
        package = next((self.root / "public/v1/packages").rglob("*.zip"))
        digest_before = package.read_bytes()
        self.run_tool("set-github-repository", "--github-repository", "Example/NewArsenalRepository")
        catalog = json.loads((self.root / "public/v1/catalog.json").read_text(encoding="utf-8"))
        self.assertTrue(catalog["Items"][0]["DownloadUrl"].startswith(
            "https://github.com/Example/NewArsenalRepository/releases/download/",
        ))
        self.assertEqual(package.read_bytes(), digest_before)

    def test_reinitialization_is_never_destructive(self) -> None:
        self.initialize()
        result = self.run_tool(
            "init", "--github-repository", "Example/Replacement", success=False,
        )
        self.assertIn("already initialized", result.stderr)

    def test_parent_and_https_validation(self) -> None:
        self.initialize()
        result = self.run_tool(
            "add", "--engine", "generals", "--type", "addon", "--id", "addon",
            "--parent", "missing-mod", "--name", "Addon", "--version", "1",
            "--package", str(self.package), success=False,
        )
        self.assertIn("unknown parent", result.stderr)
        result = self.run_tool(
            "add", "--engine", "generals", "--type", "mod", "--id", "unsafe",
            "--name", "Unsafe", "--version", "1", "--package", str(self.package),
            "--support-url", "http://example.test", success=False,
        )
        self.assertIn("HTTPS", result.stderr)

    def test_catalog_additions_need_no_launcher_source_change(self) -> None:
        self.initialize()
        self.run_tool(
            "add", "--engine", "zerohour", "--type", "mod", "--id", "dynamic-mod",
            "--name", "Dynamic Mod", "--version", "1", "--package", str(self.package),
        )
        self.run_tool(
            "add", "--engine", "zerohour", "--type", "patch", "--id", "dynamic-patch",
            "--parent", "dynamic-mod", "--name", "Dynamic Patch", "--version", "2",
            "--package", str(self.package),
        )
        self.run_tool(
            "add", "--engine", "zerohour", "--type", "addon", "--id", "dynamic-addon",
            "--parent", "dynamic-patch", "--name", "Dynamic Addon", "--version", "3",
            "--package", str(self.package),
        )
        catalog = json.loads((self.root / "public/v1/catalog.json").read_text(encoding="utf-8"))
        items = {item["Id"]: item for item in catalog["Items"]}
        self.assertEqual(set(items), {"dynamic-mod", "dynamic-patch", "dynamic-addon"})
        self.assertEqual(items["dynamic-mod"]["Type"], "mod")
        self.assertEqual(items["dynamic-patch"]["ParentId"], "dynamic-mod")
        self.assertEqual(items["dynamic-addon"]["ParentId"], "dynamic-patch")


if __name__ == "__main__":
    unittest.main()
