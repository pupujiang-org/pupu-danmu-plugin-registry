# Catalog submission and review

## Submit an entry

1. Publish an immutable versioned ZIP from the plugin source owner's GitHub Release. Do not upload the ZIP or source to this registry.
2. Create one JSON metadata file under `catalog/official/` or `catalog/community/<github-publisher>/` using `schemas/catalog-entry.schema.json`.
3. Copy the full validated Manifest into the entry. Set the exact GitHub Release URL, byte size, and lowercase SHA-256 of the ZIP.
4. Run `python scripts/build_index.py --check --verify-assets` and open a pull request. Automated checks inspect the ZIP as bounded static bytes and never execute it.

## Maintainer review

Before merging a community entry, independently verify the publisher's GitHub account/repository control and the submitter's authority to publish updates. Review the package's provenance, license, requested host capabilities, declared resources, prompts as low-trust input, and relevant update history. Keep the review evidence in the pull request. A green static check does not authenticate the author or establish that claims in the plugin are true.

The registry does not claim Ed25519 signatures, cryptographic publisher identity, or automatic revocation. HTTPS and the exact SHA-256 protect the downloaded bytes relative to the reviewed index; they do not replace maintainer review. If ownership changes or a release is compromised, remove or replace the entry through a reviewed pull request and document user-notification needs before claiming revocation support.

## Update and removal

Publish updates as a new immutable Release and a separate metadata pull request. Do not move or overwrite an existing tag. Removal is a reviewed index change; maintainers should assess installed-user impact and provide notice. Catalog inclusion never authorizes plugin code execution, network access, credential access, browser access, or direct sending.
