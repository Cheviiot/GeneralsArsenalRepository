#!/usr/bin/env python3
"""Build and validate a static Generals: Arsenal content repository.

The public tree is deliberately database-free. Packages and covers are stored
under their SHA-256 digest, while the catalog is replaced atomically after all
objects have been published and verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SCHEMA_VERSION = 1
CATALOG_PATH = Path("public/v1/catalog.json")
STATE_PATH = Path("state/items.json")
CONFIG_PATH = Path("repository.json")
PACKAGE_EXTENSIONS = {".zip", ".7z", ".rar", ".big", ".tar", ".gz", ".xz"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{0,95}$")
GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}$")
GITHUB_RELEASE_ASSET_LIMIT = 2 * 1024 * 1024 * 1024


class RepositoryError(RuntimeError):
    """A deterministic repository validation failure."""


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RepositoryError(f"missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RepositoryError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RepositoryError(f"expected a JSON object in {path}")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(prefix=path.name + ".", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def validate_https(value: str, label: str, *, optional: bool = True) -> str:
    value = value.strip()
    if not value and optional:
        return ""
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise RepositoryError(f"{label} must be an HTTPS URL without embedded credentials")
    return value.rstrip("/")


def validate_id(value: str, label: str) -> str:
    value = value.strip().lower()
    if not SAFE_ID.fullmatch(value):
        raise RepositoryError(f"{label} must match {SAFE_ID.pattern}")
    return value


def validate_github_repository(value: str) -> str:
    value = value.strip()
    if not GITHUB_REPOSITORY.fullmatch(value) or value.startswith(".") or "/." in value:
        raise RepositoryError("github_repository must use the Owner/Repository form")
    return value


def validate_text(value: str, label: str, maximum: int = 160) -> str:
    value = value.strip()
    if not value or len(value) > maximum or any(ord(character) < 32 for character in value):
        raise RepositoryError(f"{label} must contain 1-{maximum} printable characters")
    return value


def load_config(root: Path) -> dict[str, Any]:
    config = read_json(root / CONFIG_PATH)
    if config.get("schema_version") != SCHEMA_VERSION:
        raise RepositoryError("unsupported repository configuration schema")
    config["source_id"] = validate_id(str(config.get("source_id", "")), "source_id")
    config["github_repository"] = validate_github_repository(str(config.get("github_repository", "")))
    return config


def load_state(root: Path) -> dict[str, Any]:
    state = read_json(root / STATE_PATH)
    if state.get("schema_version") != SCHEMA_VERSION or not isinstance(state.get("items"), list):
        raise RepositoryError("unsupported or malformed repository state")
    return state


def object_path(kind: str, digest: str, extension: str) -> Path:
    return Path("public/v1") / kind / digest[:2] / f"{digest}{extension}"


def github_root(config: dict[str, Any]) -> str:
    return f"https://github.com/{config['github_repository']}"


def github_catalog_url(config: dict[str, Any]) -> str:
    return f"{github_root(config)}/releases/latest/download/catalog.json"


def github_object_url(config: dict[str, Any], relative: str) -> str:
    path = Path(relative)
    digest = path.stem
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise RepositoryError(f"object path is not content-addressed: {relative}")
    return f"{github_root(config)}/releases/download/objects-{digest[:2]}/{path.name}"


def publish_object(root: Path, source: Path, kind: str, allowed_extensions: set[str]) -> tuple[Path, str, int]:
    if not source.is_file() or source.is_symlink():
        raise RepositoryError(f"input must be a regular file: {source}")
    extension = source.suffix.lower()
    if extension not in allowed_extensions:
        raise RepositoryError(f"unsupported {kind} extension: {extension or '<none>'}")
    digest, size = sha256_file(source)
    if size <= 0:
        raise RepositoryError(f"empty {kind} objects are not allowed")
    if size > GITHUB_RELEASE_ASSET_LIMIT:
        raise RepositoryError(f"{kind} exceeds the 2 GiB GitHub Release asset limit")
    relative = object_path(kind, digest, extension)
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        existing_digest, existing_size = sha256_file(destination)
        if existing_digest != digest or existing_size != size:
            raise RepositoryError(f"content-address collision at {destination}")
        return relative, digest, size
    temporary = destination.with_name(destination.name + ".publishing")
    with source.open("rb") as input_file, temporary.open("wb") as output_file:
        shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
        output_file.flush()
        os.fsync(output_file.fileno())
    copied_digest, copied_size = sha256_file(temporary)
    if copied_digest != digest or copied_size != size:
        temporary.unlink(missing_ok=True)
        raise RepositoryError("published object failed its post-copy integrity check")
    os.replace(temporary, destination)
    return relative, digest, size


def item_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return str(item["engine"]), str(item["id"]), str(item["version"])


def catalog_item(item: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "Id": item["id"],
        "Engine": item["engine"],
        "Type": item["type"],
        "Name": item["name"],
        "Version": item["version"],
        "DownloadUrl": github_object_url(config, item["package_path"]),
        "SHA256": item["sha256"],
        "Size": item["size"],
    }
    optional = {
        "ParentId": "parent_id",
        "CoverUrl": "cover_path",
        "ModDBLink": "moddb_url",
        "DiscordLink": "discord_url",
        "NewsLink": "news_url",
        "SupportLink": "support_url",
    }
    for catalog_name, state_name in optional.items():
        value = item.get(state_name, "")
        if value:
            result[catalog_name] = github_object_url(config, value) if state_name == "cover_path" else value
    return result


def render_catalog(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    items = sorted(state["items"], key=item_key)
    return {
        "SchemaVersion": SCHEMA_VERSION,
        "SourceId": config["source_id"],
        "Items": [catalog_item(item, config) for item in items],
    }


def verify_state(root: Path, state: dict[str, Any], *, metadata_only: bool = False) -> None:
    seen: set[tuple[str, str, str]] = set()
    known_ids: set[tuple[str, str]] = set()
    for item in state["items"]:
        if not isinstance(item, dict):
            raise RepositoryError("state contains a non-object item")
        engine = str(item.get("engine", ""))
        item_type = str(item.get("type", ""))
        identifier = validate_id(str(item.get("id", "")), "item id")
        version = validate_text(str(item.get("version", "")), "version", 128)
        validate_text(str(item.get("name", "")), "name")
        if engine not in {"generals", "zerohour"} or item_type not in {"mod", "patch", "addon"}:
            raise RepositoryError(f"invalid engine or type for {identifier}@{version}")
        key = (engine, identifier, version)
        if key in seen:
            raise RepositoryError(f"duplicate item: {engine}/{identifier}@{version}")
        seen.add(key)
        known_ids.add((engine, identifier))
        relative = Path(str(item.get("package_path", "")))
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("packages",):
            raise RepositoryError(f"unsafe package path for {identifier}@{version}")
        expected_digest = str(item.get("sha256", ""))
        expected_size = item.get("size")
        if (len(expected_digest) != 64 or any(character not in "0123456789abcdef" for character in expected_digest)
                or relative.stem != expected_digest or not isinstance(expected_size, int)
                or expected_size <= 0 or expected_size > GITHUB_RELEASE_ASSET_LIMIT):
            raise RepositoryError(f"package integrity mismatch for {identifier}@{version}")
        if not metadata_only:
            package = root / "public/v1" / relative
            digest, size = sha256_file(package)
            if digest != expected_digest or size != expected_size:
                raise RepositoryError(f"package integrity mismatch for {identifier}@{version}")
        cover_path = str(item.get("cover_path", ""))
        if cover_path:
            relative_cover = Path(cover_path)
            if relative_cover.is_absolute() or ".." in relative_cover.parts or relative_cover.parts[:1] != ("covers",):
                raise RepositoryError(f"unsafe cover path for {identifier}@{version}")
            if len(relative_cover.stem) != 64 or any(
                    character not in "0123456789abcdef" for character in relative_cover.stem):
                raise RepositoryError(f"cover integrity mismatch for {identifier}@{version}")
            if not metadata_only:
                cover = root / "public/v1" / relative_cover
                if not cover.is_file():
                    raise RepositoryError(f"missing cover for {identifier}@{version}")
                cover_digest, cover_size = sha256_file(cover)
                if cover.stem != cover_digest or cover_size > GITHUB_RELEASE_ASSET_LIMIT:
                    raise RepositoryError(f"cover integrity mismatch for {identifier}@{version}")
        for field in ("moddb_url", "discord_url", "news_url", "support_url"):
            validate_https(str(item.get(field, "")), field)
    for item in state["items"]:
        parent = str(item.get("parent_id", ""))
        if parent and (str(item["engine"]), parent) not in known_ids:
            raise RepositoryError(f"unknown parent {parent} for {item['id']}@{item['version']}")


def command_init(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / CONFIG_PATH
    state_path = root / STATE_PATH
    if config_path.exists() or state_path.exists():
        raise RepositoryError("repository is already initialized; use set-github-repository to change its destination")
    config = {
        "schema_version": SCHEMA_VERSION,
        "source_id": validate_id(args.source_id, "source_id"),
        "github_repository": validate_github_repository(args.github_repository),
    }
    atomic_json(config_path, config)
    atomic_json(state_path, {"schema_version": SCHEMA_VERSION, "items": []})
    (root / "public/v1/packages").mkdir(parents=True, exist_ok=True)
    (root / "public/v1/covers").mkdir(parents=True, exist_ok=True)
    atomic_json(root / CATALOG_PATH, render_catalog(config, load_state(root)))
    (root / "public/healthz").write_text("ok\n", encoding="utf-8")


def command_set_github_repository(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    config = load_config(root)
    state = load_state(root)
    config["github_repository"] = validate_github_repository(args.github_repository)
    verify_state(root, state)
    atomic_json(root / CONFIG_PATH, config)
    atomic_json(root / CATALOG_PATH, render_catalog(config, state))


def command_add(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    config = load_config(root)
    state = load_state(root)
    identifier = validate_id(args.id, "id")
    engine = args.engine
    item_type = args.type
    version = validate_text(args.version, "version", 128)
    name = validate_text(args.name, "name")
    parent = validate_id(args.parent, "parent") if args.parent else ""
    if item_type == "mod" and parent:
        raise RepositoryError("a primary mod cannot have a parent")
    package_relative, digest, size = publish_object(root, args.package.resolve(), "packages", PACKAGE_EXTENSIONS)
    cover_relative = ""
    if args.cover:
        cover_relative = publish_object(root, args.cover.resolve(), "covers", IMAGE_EXTENSIONS)[0].relative_to("public/v1").as_posix()
    item = {
        "id": identifier,
        "engine": engine,
        "type": item_type,
        "parent_id": parent,
        "name": name,
        "version": version,
        "package_path": package_relative.relative_to("public/v1").as_posix(),
        "sha256": digest,
        "size": size,
        "cover_path": cover_relative,
        "moddb_url": validate_https(args.moddb_url, "moddb_url"),
        "discord_url": validate_https(args.discord_url, "discord_url"),
        "news_url": validate_https(args.news_url, "news_url"),
        "support_url": validate_https(args.support_url, "support_url"),
    }
    key = item_key(item)
    matches = [index for index, existing in enumerate(state["items"]) if item_key(existing) == key]
    if matches and not args.replace:
        raise RepositoryError(f"item already exists: {engine}/{identifier}@{version}")
    if matches:
        state["items"][matches[0]] = item
    else:
        state["items"].append(item)
    verify_state(root, state)
    atomic_json(root / STATE_PATH, state)
    atomic_json(root / CATALOG_PATH, render_catalog(config, state))


def command_remove(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    config = load_config(root)
    state = load_state(root)
    identifier = validate_id(args.id, "id")
    before = len(state["items"])
    state["items"] = [item for item in state["items"] if not (
        item["engine"] == args.engine and item["id"] == identifier and item["version"] == args.version
    )]
    if len(state["items"]) == before:
        raise RepositoryError(f"item not found: {args.engine}/{identifier}@{args.version}")
    verify_state(root, state)
    atomic_json(root / STATE_PATH, state)
    atomic_json(root / CATALOG_PATH, render_catalog(config, state))


def command_publish(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    config = load_config(root)
    state = load_state(root)
    verify_state(root, state)
    atomic_json(root / CATALOG_PATH, render_catalog(config, state))


def command_verify(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    config = load_config(root)
    state = load_state(root)
    verify_state(root, state, metadata_only=args.metadata_only)
    expected = render_catalog(config, state)
    actual = read_json(root / CATALOG_PATH)
    if actual != expected:
        raise RepositoryError("published catalog does not match repository state; run publish")
    scope = "metadata" if args.metadata_only else "all referenced objects"
    print(f"OK: {len(state['items'])} catalog entries and {scope} are valid")


def run_gh(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["gh", *arguments], check=False, text=True, capture_output=True,
        )
    except FileNotFoundError as exc:
        raise RepositoryError("GitHub CLI 'gh' is required for publication") from exc
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown GitHub CLI failure"
        raise RepositoryError(message)
    return result


def github_release_assets(repository: str, tag: str) -> list[dict[str, Any]] | None:
    result = run_gh(
        ["release", "view", tag, "--repo", repository, "--json", "assets"], check=False,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RepositoryError(f"invalid GitHub release response for {tag}") from exc
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise RepositoryError(f"GitHub release {tag} has malformed asset metadata")
    return [asset for asset in assets if isinstance(asset, dict)]


def ensure_github_release(repository: str, tag: str, title: str, notes: str) -> list[dict[str, Any]]:
    assets = github_release_assets(repository, tag)
    if assets is not None:
        return assets
    run_gh([
        "release", "create", tag, "--repo", repository, "--title", title,
        "--notes", notes, "--latest=false",
    ])
    assets = github_release_assets(repository, tag)
    if assets is None:
        raise RepositoryError(f"GitHub release was not visible after creation: {tag}")
    return assets


def ensure_github_asset(repository: str, root: Path, relative: str) -> None:
    path = root / "public/v1" / relative
    digest, size = sha256_file(path)
    if digest != path.stem or size <= 0 or size > GITHUB_RELEASE_ASSET_LIMIT:
        raise RepositoryError(f"refusing to upload an invalid object: {path}")
    tag = f"objects-{digest[:2]}"
    assets = ensure_github_release(
        repository, tag, f"Immutable objects {digest[:2]}",
        "Content-addressed Generals: Arsenal packages and covers. Assets in this release are immutable.",
    )
    matches = [asset for asset in assets if asset.get("name") == path.name]
    if matches:
        remote_size = matches[0].get("size")
        remote_digest = str(matches[0].get("digest") or "")
        if remote_size != size or (remote_digest and remote_digest != f"sha256:{digest}"):
            raise RepositoryError(f"remote immutable asset does not match local object: {path.name}")
        return
    run_gh(["release", "upload", tag, f"{path}#{path.name}", "--repo", repository])
    assets = github_release_assets(repository, tag)
    matches = [asset for asset in (assets or []) if asset.get("name") == path.name]
    if not matches or matches[0].get("size") != size:
        raise RepositoryError(f"GitHub did not confirm uploaded asset: {path.name}")
    remote_digest = str(matches[0].get("digest") or "")
    if remote_digest and remote_digest != f"sha256:{digest}":
        raise RepositoryError(f"GitHub reported a wrong digest for uploaded asset: {path.name}")


def command_publish_github(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    config = load_config(root)
    state = load_state(root)
    verify_state(root, state)
    repository = config["github_repository"]
    viewed = run_gh(["repo", "view", repository, "--json", "nameWithOwner"], check=False)
    if viewed.returncode != 0:
        raise RepositoryError(f"GitHub repository is not accessible: {repository}")
    atomic_json(root / CATALOG_PATH, render_catalog(config, state))
    object_paths = {
        str(item[field])
        for item in state["items"]
        for field in ("package_path", "cover_path")
        if item.get(field)
    }
    for relative in sorted(object_paths):
        ensure_github_asset(repository, root, relative)
    catalog = root / CATALOG_PATH
    catalog_digest, _ = sha256_file(catalog)
    catalog_tag = f"catalog-{catalog_digest[:16]}"
    assets = github_release_assets(repository, catalog_tag)
    if assets is None:
        run_gh([
            "release", "create", catalog_tag, f"{catalog}#catalog.json", "--repo", repository,
            "--title", f"Arsenal catalog {catalog_digest[:12]}",
            "--notes", "Validated RepositoryCatalogV1 snapshot for Generals: Arsenal.", "--latest",
        ])
    else:
        matches = [asset for asset in assets if asset.get("name") == "catalog.json"]
        if not matches:
            raise RepositoryError(f"existing catalog release is missing catalog.json: {catalog_tag}")
        run_gh(["release", "edit", catalog_tag, "--repo", repository, "--latest"])
    print(f"OK: published {len(object_paths)} immutable objects")
    print(f"OK: catalog: {github_catalog_url(config)}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", type=Path, default=Path("data"), help="repository data directory")
    commands = result.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="initialize an empty repository")
    init.add_argument("--source-id", default="arsenal")
    init.add_argument("--github-repository", required=True)
    init.set_defaults(handler=command_init)

    set_repository = commands.add_parser(
        "set-github-repository", help="change the destination GitHub repository and rebuild the catalog",
    )
    set_repository.add_argument("--github-repository", required=True)
    set_repository.set_defaults(handler=command_set_github_repository)

    add = commands.add_parser("add", help="atomically add or replace a package version")
    add.add_argument("--engine", choices=("generals", "zerohour"), required=True)
    add.add_argument("--type", choices=("mod", "patch", "addon"), required=True)
    add.add_argument("--id", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--version", required=True)
    add.add_argument("--package", type=Path, required=True)
    add.add_argument("--cover", type=Path)
    add.add_argument("--parent", default="")
    add.add_argument("--moddb-url", default="")
    add.add_argument("--discord-url", default="")
    add.add_argument("--news-url", default="")
    add.add_argument("--support-url", default="")
    add.add_argument("--replace", action="store_true")
    add.set_defaults(handler=command_add)

    remove = commands.add_parser("remove", help="unpublish a version without deleting immutable objects")
    remove.add_argument("--engine", choices=("generals", "zerohour"), required=True)
    remove.add_argument("--id", required=True)
    remove.add_argument("--version", required=True)
    remove.set_defaults(handler=command_remove)

    publish = commands.add_parser("publish", help="verify state and atomically rebuild the catalog")
    publish.set_defaults(handler=command_publish)
    publish_github = commands.add_parser(
        "publish-github", help="upload immutable objects and atomically publish the latest catalog release",
    )
    publish_github.set_defaults(handler=command_publish_github)
    verify = commands.add_parser("verify", help="verify catalog and all referenced objects")
    verify.add_argument("--metadata-only", action="store_true")
    verify.set_defaults(handler=command_verify)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        args.handler(args)
    except (OSError, RepositoryError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
