# Pupu Danmu plugin registry

This repository contains only plugin Catalog metadata, JSON schemas, validation tooling, and the generated GitHub Pages index. It must never contain plugin source code or ZIP packages. Official package source and immutable Release assets belong in [`pupu-danmu-plugins`](https://github.com/pupujiang-org/pupu-danmu-plugins); community authors publish packages from their own repositories.

The public index is served at:

`https://pupujiang-org.github.io/pupu-danmu-plugin-registry/v1/index.json`

## Layout

- `catalog/official/<plugin>.json`: reviewed official package metadata.
- `catalog/community/<publisher>/<plugin>.json`: reviewed community package metadata.
- `schemas/`: schema-v1 Manifest and Catalog entry contracts.
- `scripts/build_index.py`: static metadata and optional ZIP/SHA validation; never imports or executes package contents.
- `scripts/spdx-license-ids.json`: the known SPDX license and exception identifiers used to validate declared package licenses.
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

The declared license is checked, not assumed. An entry under `catalog/official/` must declare `MIT` and the referenced package manifest must match, while an entry under `catalog/community/` may declare any license its author chooses as long as the Manifest `license` resolves to a known SPDX identifier or expression (for example `Apache-2.0`, `GPL-3.0-or-later`, or `MIT OR Apache-2.0`, plus `LicenseRef-…` for a license with no SPDX id). An empty, missing, `NONE`, `NOASSERTION`, or unrecognized license fails validation.

## License

This repository's own material — the validation tooling, the JSON schemas, the documentation, and the generated index — is MIT-licensed; see [LICENSE](LICENSE).

The repository does not relicense what it catalogs. A Catalog entry is a description that points at a package hosted somewhere else, so a catalogued package keeps whatever license its authors declare and is not relicensed by being listed here. Official packages published from [`pupu-danmu-plugins`](https://github.com/pupujiang-org/pupu-danmu-plugins) are MIT; the host application (`pupu-danmu`) is licensed separately and is not covered by either repository's license.

`scripts/spdx-license-ids.json` is generated from the SPDX license list data at <https://github.com/spdx/license-list-data>, which is published under CC0-1.0.
