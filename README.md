# Pupu Danmu plugin registry

This repository contains only plugin Catalog metadata, validation tooling, and the generated GitHub Pages index. It must never contain plugin source code or ZIP packages. Each official plugin has its own source repository and immutable Release assets: [fabric-buyer](https://github.com/pupujiang-org/pupu-danmu-plugin-fabric-buyer) and [music-audience](https://github.com/pupujiang-org/pupu-danmu-plugin-music-audience). Community authors publish packages from their own repositories.

The host's [plugin developer guide](https://github.com/pupujiang-org/pupu-danmu/blob/main/docs/public/plugin-development.md) documents declarative Manifest v1 and Python Worker Manifest v2 behavior, package limits, license requirements and install flow.

The public index is served at:

`https://pupujiang-org.github.io/pupu-danmu-plugin-registry/v1/index.json`

## Layout

- `catalog/official/<plugin>.json`: reviewed official package metadata.
- `catalog/community/<publisher>/<plugin>.json`: reviewed community package metadata.
- `catalog/workers/official/<worker>.json` and `catalog/workers/community/<publisher>/<worker>.json`: reviewed Python Worker metadata.
- `schemas/`: declarative Manifest v1 and Python Worker Manifest v2 contracts, plus their Catalog entry schemas.
- `scripts/build_index.py`: static metadata, Python syntax, package-layout and optional ZIP/SHA validation; never imports or executes package contents.
- `scripts/spdx-license-ids.json`: the known SPDX license and exception identifiers used to validate declared package licenses.
- `.github/workflows/pages.yml`: builds and deploys the index from `main`.

The index keeps the existing `plugins` array and includes a `workers` array populated only from reviewed immutable Release assets. A plugin ID may have one current release in each runtime family, so a declarative release and a Worker release can coexist at different versions. The exact `(manifest.id, manifest.version)` pair must be unique across both arrays, and each array's runtime type is checked; this makes an exact session pin resolve to one runtime. Registry merge does not imply that the host application has completed physical UI or real-room acceptance, or that catalogued Worker code is enabled for execution.

## Local check

Use Python 3.13 or newer:

```powershell
python scripts/build_index.py --write-index
python scripts/build_index.py --check --verify-assets
python scripts/build_index.py --site-dir site
```

After adding or changing a Catalog entry, regenerate the committed index with the first command. The second command validates all entries and downloads only bounded HTTPS GitHub Release ZIP assets to verify exact size, SHA-256, package layout, and manifest agreement. Worker source files are parsed as UTF-8 Python syntax but never imported or executed. The third command builds a Pages directory.

## Review policy

Official and community entries use the same index and package protocol. Community entries are never trusted because of their label: maintainers review the submitter's control of the publisher account/repository, package contents, declared license and host capabilities, and update ownership. The automated workflow is static-only and runs without write secrets on pull requests. See [CONTRIBUTING.md](CONTRIBUTING.md) for the review steps.

The declared license is checked, not assumed. Existing declarative entries under `catalog/official/` must declare `MIT`, use the exact official repository mapped to their plugin ID, and match the referenced package manifest. Official Worker entries use the generic `org.pupujiang.<slug>` ID and `pupujiang-org/pupu-danmu-plugin-<slug>` Release rule and must also declare `MIT`. Community entries may declare any license their author chooses as long as the Manifest `license` resolves to a known SPDX identifier or expression (for example `Apache-2.0`, `GPL-3.0-or-later`, or `MIT OR Apache-2.0`, plus `LicenseRef-…` for a license with no SPDX id). An empty, missing, `NONE`, `NOASSERTION`, or unrecognized license fails validation.

## License

This repository's own material — the validation tooling, the JSON schemas, the documentation, and the generated index — is MIT-licensed; see [LICENSE](LICENSE).

The repository does not relicense what it catalogs. A Catalog entry points at a package hosted somewhere else, so a catalogued package keeps whatever license its authors declare. Each official plugin repository and package is MIT; the host application (`pupu-danmu`) is licensed separately.

`scripts/spdx-license-ids.json` is generated from the SPDX license list data at <https://github.com/spdx/license-list-data>, which is published under CC0-1.0.
