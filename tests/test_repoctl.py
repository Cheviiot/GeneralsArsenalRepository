#!/usr/bin/env python3
"""Tests for the dependency-free Arsenal repository publisher."""

from __future__ import annotations

import json
import hashlib
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
            "add", "--engine", "zerohour", "--type", "addon", "--id", "dynamic-rival",
            "--parent", "dynamic-mod", "--name", "Dynamic Rival", "--version", "1",
            "--package", str(self.package), "--requires", "dynamic-mod@1",
        )
        self.run_tool(
            "add", "--engine", "zerohour", "--type", "patch", "--id", "dynamic-patch",
            "--parent", "dynamic-mod", "--name", "Dynamic Patch", "--version", "2",
            "--package", str(self.package), "--requires", "dynamic-mod@1",
        )
        self.run_tool(
            "add", "--engine", "zerohour", "--type", "addon", "--id", "dynamic-addon",
            "--parent", "dynamic-patch", "--name", "Dynamic Addon", "--version", "3",
            "--package", str(self.package), "--requires", "dynamic-patch@2",
            "--conflicts", "dynamic-rival",
        )
        catalog = json.loads((self.root / "public/v1/catalog.json").read_text(encoding="utf-8"))
        items = {item["Id"]: item for item in catalog["Items"]}
        self.assertEqual(set(items), {"dynamic-mod", "dynamic-patch", "dynamic-addon", "dynamic-rival"})
        self.assertEqual(items["dynamic-mod"]["Type"], "mod")
        self.assertEqual(items["dynamic-patch"]["ParentId"], "dynamic-mod")
        self.assertEqual(items["dynamic-patch"]["Requires"], ["dynamic-mod@1"])
        self.assertEqual(items["dynamic-addon"]["ParentId"], "dynamic-patch")
        self.assertEqual(items["dynamic-addon"]["Requires"], ["dynamic-patch@2"])
        self.assertEqual(items["dynamic-addon"]["Conflicts"], ["dynamic-rival"])

    def test_author_hosted_file_set_is_published_without_mirroring_content(self) -> None:
        self.initialize()
        first = b"first immutable file"
        second = b"second immutable file"
        manifest = Path(self.temporary.name) / "external-files.json"
        manifest.write_text(json.dumps({"files": [
            {
                "path": "!AuthorCore.gib",
                "url": "https://author.example.invalid/releases/core.gib",
                "sha256": hashlib.sha256(first).hexdigest(),
                "size": len(first),
            },
            {
                "path": "Data/Maps.big",
                "url": "https://author.example.invalid/releases/maps.big",
                "sha256": hashlib.sha256(second).hexdigest(),
                "size": len(second),
            },
        ]}), encoding="utf-8")
        self.run_tool(
            "add", "--engine", "zerohour", "--type", "mod", "--id", "author-mod",
            "--name", "Author Mod", "--version", "1.0", "--external-manifest", str(manifest),
        )
        catalog = json.loads((self.root / "public/v1/catalog.json").read_text(encoding="utf-8"))
        item = catalog["Items"][0]
        self.assertNotIn("DownloadUrl", item)
        self.assertNotIn("SHA256", item)
        self.assertEqual([entry["Path"] for entry in item["Files"]], ["!AuthorCore.gib", "Data/Maps.big"])
        self.assertFalse(any((self.root / "public/v1/packages").rglob("*")))
        self.run_tool("verify")

    def test_author_hosted_archive_is_published_without_mirroring_content(self) -> None:
        self.initialize()
        payload = b"immutable author archive"
        manifest = Path(self.temporary.name) / "external-archive.json"
        manifest.write_text(json.dumps({"archive": {
            "url": "https://author.example.invalid/releases/patch.zip",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }}), encoding="utf-8")
        self.run_tool(
            "add", "--engine", "zerohour", "--type", "mod", "--id", "author-archive",
            "--name", "Author Archive", "--version", "1.0", "--external-manifest", str(manifest),
        )
        catalog = json.loads((self.root / "public/v1/catalog.json").read_text(encoding="utf-8"))
        item = catalog["Items"][0]
        self.assertEqual(item["DownloadUrl"], "https://author.example.invalid/releases/patch.zip")
        self.assertEqual(item["SHA256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(item["Size"], len(payload))
        self.assertNotIn("Files", item)
        self.assertFalse(any((self.root / "public/v1/packages").rglob("*")))
        self.run_tool("verify")

    def test_local_generator_is_published_without_a_package(self) -> None:
        self.initialize()
        self.run_tool(
            "add", "--engine", "zerohour", "--type", "mod", "--id", "base-mod",
            "--name", "Base Mod", "--version", "1", "--package", str(self.package),
        )
        self.run_tool(
            "add", "--engine", "zerohour", "--type", "addon", "--id", "russian-bridge",
            "--parent", "base-mod", "--name", "Russian Bridge", "--version", "1",
            "--generator", "retail-russian-merge-v1", "--requires", "base-mod@1",
        )
        catalog = json.loads((self.root / "public/v1/catalog.json").read_text(encoding="utf-8"))
        bridge = next(item for item in catalog["Items"] if item["Id"] == "russian-bridge")
        self.assertEqual(bridge["Generator"], "retail-russian-merge-v1")
        self.assertNotIn("DownloadUrl", bridge)
        self.assertNotIn("Files", bridge)
        self.run_tool("verify")

    def test_ambiguous_external_delivery_is_rejected(self) -> None:
        self.initialize()
        manifest = Path(self.temporary.name) / "ambiguous-external.json"
        manifest.write_text(json.dumps({
            "archive": {
                "url": "https://author.example.invalid/releases/patch.zip",
                "sha256": "0" * 64,
                "size": 1,
            },
            "files": [{
                "path": "patch.big",
                "url": "https://author.example.invalid/releases/patch.big",
                "sha256": "0" * 64,
                "size": 1,
            }],
        }), encoding="utf-8")
        result = self.run_tool(
            "add", "--engine", "zerohour", "--type", "mod", "--id", "ambiguous",
            "--name", "Ambiguous", "--version", "1", "--external-manifest", str(manifest), success=False,
        )
        self.assertIn("exactly one archive or file set", result.stderr)

    def test_unsafe_external_file_set_is_rejected(self) -> None:
        self.initialize()
        manifest = Path(self.temporary.name) / "unsafe-external-files.json"
        manifest.write_text(json.dumps({"files": [{
            "path": "../escape.dll",
            "url": "https://author.example.invalid/escape.dll",
            "sha256": "0" * 64,
            "size": 1,
        }]}), encoding="utf-8")
        result = self.run_tool(
            "add", "--engine", "zerohour", "--type", "mod", "--id", "unsafe-external",
            "--name", "Unsafe", "--version", "1", "--external-manifest", str(manifest), success=False,
        )
        self.assertIn("safe relative path", result.stderr)

    def test_unknown_and_self_compatibility_selectors_are_rejected(self) -> None:
        self.initialize()
        result = self.run_tool(
            "add", "--engine", "zerohour", "--type", "mod", "--id", "broken",
            "--name", "Broken", "--version", "1", "--package", str(self.package),
            "--requires", "missing@1", success=False,
        )
        self.assertIn("unknown requires selector", result.stderr)
        result = self.run_tool(
            "add", "--engine", "zerohour", "--type", "mod", "--id", "self",
            "--name", "Self", "--version", "1", "--package", str(self.package),
            "--conflicts", "self", success=False,
        )
        self.assertIn("self-referencing conflicts selector", result.stderr)

    def test_candidate_graph_is_validated_without_publishing_packages(self) -> None:
        self.initialize()
        candidates = self.root / "candidates"
        candidates.mkdir(parents=True, exist_ok=True)
        (candidates / "example.json").write_text(json.dumps({
            "schema_version": 1,
            "engine": "zerohour",
            "items": [
                {
                    "id": "base", "type": "mod", "name": "Base", "version": "1.0",
                    "status": "permission-required", "project_url": "https://example.invalid/base",
                },
                {
                    "id": "patch", "type": "patch", "name": "Patch", "version": "2.0",
                    "parent_id": "base", "requires": ["base@1.0"], "conflicts": [],
                    "status": "permission-required", "project_url": "https://example.invalid/patch",
                },
            ],
        }), encoding="utf-8")
        result = self.run_tool("verify")
        self.assertIn("2 candidate entries", result.stdout)
        payload = json.loads((self.root / "public/v1/catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["Items"], [])


if __name__ == "__main__":
    unittest.main()
