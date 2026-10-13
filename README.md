# Pupu Danmu plugin registry

This repository contains only plugin Catalog metadata, JSON schemas, validation tooling, and the generated GitHub Pages index. It must never contain plugin source code or ZIP packages. Official package source and immutable Release assets belong in [`pupu-danmu-plugins`](https://github.com/pupujiang-org/pupu-danmu-plugins); community authors publish packages from their own repositories.

The public index is served at:

`https://pupujiang-org.github.io/pupu-danmu-plugin-registry/v1/index.json`

## Layout

- `catalog/official/<plugin>.json`: reviewed official package metadata.
- `catalog/community/<publisher>/<plugin>.json`: reviewed community package metadata.
- `schemas/`: schema-v1 Manifest and Catalog entry contracts.
- `scripts/build_index.py`: static metadata and optional ZIP/SHA validation; never imports or executes package contents.
- `.github/workflows/pages.yml`: builds and deploys the index from `main`.

An empty index is intentional until a package is compatible with the host runtime and its immutable Release asset has been reviewed. Registry merge does not imply that the host application has completed physical UI or real-room acceptance.

## Local check

Use Python 3.13 or newer:

```powershell
python scripts/build_index.py --write-index
python scripts/build_index.py --check --verify-assets
python scripts/build_index.py --site-dir site
```

After adding or changing a Catalog entry, regenerate the committed index with the first command. The second command validates all entries and downloads only bounded HTTPS GitHub Release ZIP assets to verify exact size, SHA-256, package layout, and manifest agreement. It never extracts or executes them. The third command builds a Pages directory.

## Review policy

Official and community entries use the same index and package protocol. Community entries are never trusted because of their label: maintainers review the submitter's control of the publisher account/repository, package contents, declared license and host capabilities, and update ownership. The automated workflow is static-only and runs without write secrets on pull requests. See [CONTRIBUTING.md](CONTRIBUTING.md) for the review steps.
