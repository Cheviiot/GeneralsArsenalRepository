# Generals: Arsenal Repository

This is the authoritative modification repository for Generals: Arsenal. It is
hosted by the public `Cheviiot/GeneralsArsenalRepository` GitHub repository. It
does not proxy GenLauncher catalogs. Packages can either be immutable GitHub
Release assets or immutable author-hosted HTTPS file sets whose path, size, and
SHA-256 are pinned in the Arsenal catalog.

The repository is database-free:

- `data/state/items.json` is the private publication model;
- `data/public/v1/catalog.json` is the generated `RepositoryCatalogV1`;
- packages and covers are immutable SHA-256-addressed GitHub Release assets;
- the newest validated catalog is the only release marked as `Latest`.

The launcher reads:

```text
https://github.com/Cheviiot/GeneralsArsenalRepository/releases/latest/download/catalog.json
```

Catalog entries are data, not compiled launcher code. Adding, updating, or
removing a mod, patch, or add-on requires no launcher rebuild or application
release: **REFRESH** reads the newly published snapshot immediately, and the
normal background refresh reads it after the 24-hour cache lifetime.

GitHub redirects release downloads to its asset service. The native launcher
follows that redirect and retains `HEAD`, byte-range resumption, ETag, and
strong `If-Match` validation. GitHub currently limits an individual release asset to
2 GiB. `repoctl.py` rejects a larger archive before publication.

## Publishing a modification

Authenticate the GitHub CLI once:

```bash
gh auth login
```

Add a package to the local publication model:

```bash
python3 repoctl.py --root data add \
  --engine zerohour \
  --type mod \
  --id example-mod \
  --name "Example Mod" \
  --version 1.0 \
  --package /path/to/example-mod.zip \
  --cover /path/to/cover.png
```

For a release that its author already publishes as multiple files, create a
small JSON manifest instead of repackaging or mirroring it:

```json
{
  "files": [
    {
      "path": "!ExampleCore.gib",
      "url": "https://author.example/releases/!ExampleCore.gib",
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "size": 123456
    }
  ]
}
```

```bash
python3 repoctl.py --root data add \
  --engine zerohour \
  --type mod \
  --id example-author-mod \
  --name "Example Author Mod" \
  --version 1.0 \
  --external-manifest data/sources/example-author-mod.json
```

The launcher downloads up to four files concurrently, retains a separate
resumable partial for each one, verifies every SHA-256, and only then publishes
the installed modification atomically.

An author-hosted ZIP, 7z, RAR, BIG, or tar package can be referenced without
mirroring it into this repository by using the alternative archive form:

```json
{
  "archive": {
    "url": "https://author.example/releases/example-patch.zip",
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "size": 123456
  }
}
```

An external manifest must contain exactly one `archive` or `files` member.
Every download is HTTPS-only and hash-pinned; changing an author's remote file
without publishing a new version therefore fails closed instead of silently
installing different content.

Verify and publish it:

```bash
python3 repoctl.py --root data verify
python3 repoctl.py --root data publish-github
```

Publication is ordered safely:

1. Validate every local object and manifest field.
2. Upload missing packages and covers to `objects-XX` releases.
3. Confirm the remote asset size and SHA-256 when GitHub exposes its digest.
4. Generate a content-addressed catalog release.
5. Mark that complete catalog release as `Latest`.

An interrupted upload therefore cannot expose a catalog that references a
missing package. Existing assets are never overwritten. Removing a version
only removes it from the next catalog; its immutable release asset remains
available to old manifests and resumable downloads.

## Modification relationships

- A `mod` has no parent.
- A `patch` names its primary mod with `--parent`.
- An `addon` may target a mod or patch with `--parent`, or remain global.
- Repeatable `--requires id` or `--requires id@version` arguments describe
  mandatory stack members. Repeatable `--conflicts id` or
  `--conflicts id@version` arguments describe combinations the launcher must
  reject before activation.
- A data-only plug-in is published as an `addon`; native executable plug-ins
  are intentionally unsupported.
- IDs, versions, HTTPS information links, package hashes, sizes, and covers are
  validated before the catalog changes.

Use `--moddb-url`, `--discord-url`, `--news-url`, and `--support-url` only for
informational HTTPS pages. An external archive or file-set URL is accepted only together
with a non-zero size and a full SHA-256 digest; credentials and non-HTTPS URLs
are rejected.

## Candidate ecosystems

`data/candidates/*.json` records a reviewed dependency graph before any
third-party archive is accepted for publication. Candidate files contain no
package URL, hash, or mirrored content. `repoctl.py verify` validates their
parents, exact-version requirements, conflicts, source pages, and publication
status, while keeping them out of the public launcher catalog.

The first ecosystem is Rise of the Reds 1.87 PB 2.0 with separate HanPatch,
AntiThesis, and Navy branches plus their compatible add-ons. The base mod is
published as a hash-pinned set of the 18 files served by its existing official
host, so Arsenal does not redistribute or alter it. Other entries stay hidden
until their delivery and runtime compatibility are validated. AntiThesis
ControlBar Pro is additionally `runtime-blocked` because it requires GenTool,
which Arsenal does not bundle or execute.

## Verification

Run the local suite:

```bash
python3 -m unittest discover -s tests -v
python3 repoctl.py --root data verify
```

Run the real public protocol checks:

```bash
ARSENAL_REPOSITORY_CATALOG_URL=\
https://github.com/Cheviiot/GeneralsArsenalRepository/releases/latest/download/catalog.json \
  python3 -m unittest tests.test_remote_repository -v
```

GitHub Actions repeats metadata and public protocol validation after every
push. No cloud account, database, dedicated server, access key, or paid runner
is required.

Do not publish a third-party package or cover without permission from its
author. Executable extension packages remain unsupported by the Arsenal
launcher; both the publisher and launcher enforce data-only content rules.
