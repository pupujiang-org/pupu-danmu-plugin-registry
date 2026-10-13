## Plugin Catalog submission

- [ ] The plugin source and ZIP Release remain in the publisher's repository; no package is added to this registry.
- [ ] The entry is under the correct `official` or `community/<publisher>` path.
- [ ] The Release URL is immutable and the byte size/SHA-256 match the ZIP.
- [ ] The full Manifest matches schema v1 and requests only supported capabilities.
- [ ] The package contains only static allowlisted data and documentation.
- [ ] The license and publisher identity/control evidence are included for maintainer review.
- [ ] I ran `python scripts/build_index.py --check --verify-assets`.

Explain the plugin's user-facing purpose, declared capabilities, provenance, license, and any changes since its prior version. Do not include API keys, tokens, cookies, or private user data.
