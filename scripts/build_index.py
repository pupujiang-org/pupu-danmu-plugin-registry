"""Validate registry metadata and build a Pages index without executing packages."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import stat
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import UTC
from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
CATALOG_ROOT = ROOT / "catalog"
INDEX_PATH = ROOT / "v1" / "index.json"
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_PROMPT_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 5 * 1024 * 1024
MAX_FILES = 100
MAX_RATIO = 100
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PLUGIN_ID = re.compile(r"^(?:[a-z][a-z0-9-]*\.){2,}[a-z0-9][a-z0-9-]*$")
SEMVER_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    rf"(?:-({SEMVER_IDENTIFIER}(?:\.{SEMVER_IDENTIFIER})*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
TOP_LEVEL_MANIFEST_FIELDS = {
    "schema_version",
    "id",
    "name",
    "version",
    "description",
    "author",
    "license",
    "min_host_version",
    "kind",
    "runtime_type",
    "plugin_api_version",
    "capabilities",
    "config_schema",
    "recommended_pacing",
    "resources",
}
ROOT_PACKAGE_FILES = {
    "manifest.json",
    "README.md",
    "LICENSE",
    "personas.json",
    "topics.json",
    "participation.json",
}
ALLOWED_REDIRECT_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}


class ValidationError(ValueError):
    """Catalog metadata or a referenced static package failed validation."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("JSON contains a duplicate object key")
        result[key] = value
    return result


def _read_json(path: Path, *, maximum_bytes: int) -> object:
    payload = path.read_bytes()
    if len(payload) > maximum_bytes:
        raise ValidationError(f"JSON file exceeds size limit: {path.relative_to(ROOT)}")
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValidationError("JSON contains a non-finite number")
            ),
        )
    except ValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValidationError(f"invalid UTF-8 JSON: {path.relative_to(ROOT)}") from None


def _validate_manifest(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != TOP_LEVEL_MANIFEST_FIELDS:
        raise ValidationError("Manifest fields are missing or unsupported")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValidationError("unsupported manifest schema version")
    plugin_id = value["id"]
    version = value["version"]
    if not isinstance(plugin_id, str) or PLUGIN_ID.fullmatch(plugin_id) is None:
        raise ValidationError("plugin id is invalid")
    if not isinstance(version, str) or SEMVER.fullmatch(version) is None:
        raise ValidationError("plugin version is invalid")
    for field, maximum in (
        ("name", 100),
        ("description", 500),
        ("author", 100),
        ("license", 100),
        ("min_host_version", 80),
    ):
        text = value[field]
        if not isinstance(text, str) or not text.strip() or len(text) > maximum:
            raise ValidationError(f"Manifest {field} is invalid")
    if value["kind"] != "ai-participation":
        raise ValidationError("unsupported plugin kind")
    if value["runtime_type"] != "declarative":
        raise ValidationError("unsupported plugin runtime")
    if type(value["plugin_api_version"]) is not int or value["plugin_api_version"] != 1:
        raise ValidationError("unsupported plugin API version")
    capabilities = value["capabilities"]
    if (
        not isinstance(capabilities, list)
        or len(capabilities) > 16
        or not all(isinstance(item, str) for item in capabilities)
        or len(set(capabilities)) != len(capabilities)
        or not set(capabilities).issubset(
            {"chat_context", "host_llm", "persona", "topic_hints"}
        )
    ):
        raise ValidationError("plugin capabilities are invalid")
    schema = value["config_schema"]
    if not isinstance(schema, dict) or len(schema) > 32:
        raise ValidationError("plugin config schema is invalid")
    pacing = value["recommended_pacing"]
    if not isinstance(pacing, dict) or not set(pacing).issubset(
        {"min_interval_seconds", "max_interval_seconds", "hourly_limit"}
    ):
        raise ValidationError("recommended pacing is invalid")
    resources = value["resources"]
    if not isinstance(resources, dict) or len(resources) > 4:
        raise ValidationError("plugin resources are invalid")
    allowed = {
        "personas": {"personas.json"},
        "topics": {"topics.json"},
        "participation": {"participation.json"},
    }
    for kind, path in resources.items():
        if not isinstance(kind, str) or not isinstance(path, str):
            raise ValidationError("plugin resource references must be text")
        if kind == "prompt":
            valid = (
                path.startswith("prompts/")
                and path.endswith((".txt", ".md"))
                and path.count("/") == 1
            )
        else:
            valid = kind in allowed and path in allowed[kind]
        if not valid:
            raise ValidationError("plugin resource reference is unsupported")
    return value


class _SafeReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_REDIRECT_HOSTS:
            raise ValidationError(
                "Release asset redirected outside approved HTTPS hosts"
            )
        if parsed.username is not None or parsed.password is not None:
            raise ValidationError("Release asset redirect contains credentials")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_archive(payload: bytes, manifest: dict[str, object]) -> None:
    present: set[str] = set()
    contents: dict[str, bytes] = {}
    casefolded: set[str] = set()
    total_size = 0
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= MAX_FILES:
                raise ValidationError("plugin ZIP file count is invalid")
            for info in entries:
                name = info.filename
                directory = info.is_dir()
                canonical = name[:-1] if directory else name
                if (
                    not canonical
                    or "\\" in name
                    or ":" in name
                    or PurePosixPath(canonical).as_posix() != canonical
                    or canonical.startswith("/")
                    or any(part in {"", ".", ".."} for part in canonical.split("/"))
                ):
                    raise ValidationError("plugin ZIP contains an unsafe path")
                folded = canonical.casefold()
                if folded in casefolded:
                    raise ValidationError("plugin ZIP contains duplicate paths")
                casefolded.add(folded)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or info.flag_bits & 0x1:
                    raise ValidationError(
                        "plugin ZIP contains a symlink or encrypted file"
                    )
                if directory:
                    if canonical != "prompts":
                        raise ValidationError(
                            "plugin ZIP contains an unsupported directory"
                        )
                    continue
                if canonical not in ROOT_PACKAGE_FILES and not (
                    canonical.startswith("prompts/")
                    and canonical.count("/") == 1
                    and canonical.endswith((".txt", ".md"))
                ):
                    raise ValidationError("plugin ZIP contains a non-data file")
                if info.file_size > MAX_FILE_BYTES:
                    raise ValidationError("plugin ZIP contains an oversized file")
                total_size += info.file_size
                if total_size > MAX_TOTAL_BYTES:
                    raise ValidationError("plugin ZIP expands beyond the size limit")
                if info.file_size and (
                    info.compress_size == 0
                    or info.file_size > info.compress_size * MAX_RATIO
                ):
                    raise ValidationError("plugin ZIP compression ratio is excessive")
                with archive.open(info) as source:
                    content = source.read(MAX_FILE_BYTES + 1)
                if len(content) != info.file_size or len(content) > MAX_FILE_BYTES:
                    raise ValidationError("plugin ZIP entry size is inconsistent")
                present.add(canonical)
                contents[canonical] = content
                if canonical == "manifest.json":
                    package_manifest = _validate_manifest(_read_bytes_json(content))
                    if package_manifest != manifest:
                        raise ValidationError("ZIP Manifest differs from Catalog entry")
    except ValidationError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile):
        raise ValidationError("plugin ZIP is invalid") from None

    if "manifest.json" not in present:
        raise ValidationError("plugin ZIP has no manifest")
    resources = manifest["resources"]
    assert isinstance(resources, dict)
    if any(resource_path not in present for resource_path in resources.values()):
        raise ValidationError("plugin ZIP is missing a declared resource")
    for kind, resource_path in resources.items():
        content = contents[resource_path]
        if kind == "prompt":
            if len(content) > MAX_PROMPT_BYTES:
                raise ValidationError("plugin prompt exceeds the size limit")
            try:
                prompt = content.decode("utf-8")
            except UnicodeDecodeError:
                raise ValidationError("plugin prompt is not UTF-8") from None
            if "\x00" in prompt:
                raise ValidationError("plugin prompt contains invalid text")
        else:
            resource = _read_bytes_json(content)
            if not isinstance(resource, dict):
                raise ValidationError("plugin JSON resources must have an object root")


def _read_bytes_json(payload: bytes) -> object:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValidationError("manifest contains a non-finite number")
            ),
        )
    except ValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValidationError("plugin ZIP manifest is invalid JSON") from None


def _validate_release_url(
    url: object, *, origin: str, manifest: dict[str, object], path: Path
) -> str:
    if not isinstance(url, str):
        raise ValidationError("package URL must be text")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError("package URL must be a fixed HTTPS GitHub Release asset")
    parts = parsed.path.split("/")
    if len(parts) != 7 or parts[0] != "" or parts[3:5] != ["releases", "download"]:
        raise ValidationError("package URL path is invalid")
    owner, repository, tag, filename = parts[1], parts[2], parts[5], parts[6]
    plugin_id = manifest["id"]
    version = manifest["version"]
    assert isinstance(plugin_id, str) and isinstance(version, str)
    slug = plugin_id.rsplit(".", 1)[-1]
    if path.stem != slug:
        raise ValidationError("Catalog filename must match the plugin id suffix")
    if tag != f"{slug}-v{version}" or filename != f"{slug}-{version}.zip":
        raise ValidationError("Release URL does not match plugin id and version")
    relative = path.relative_to(CATALOG_ROOT).parts
    if origin == "official":
        if (
            relative[0] != "official"
            or owner != "pupujiang-org"
            or repository != "pupu-danmu-plugins"
        ):
            raise ValidationError(
                "official entry must use the official plugin Release source"
            )
    else:
        if (
            len(relative) != 3
            or relative[0] != "community"
            or owner.casefold() != relative[1].casefold()
        ):
            raise ValidationError("community publisher must match the Release owner")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", owner) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,100}", repository
        ):
            raise ValidationError("community Release owner or repository is invalid")
    return url


def _entry(path: Path, origin: str, *, verify_assets: bool) -> dict[str, object]:
    raw = _read_json(path, maximum_bytes=128 * 1024)
    if not isinstance(raw, dict) or set(raw) != {
        "manifest",
        "package_url",
        "sha256",
        "package_size",
    }:
        raise ValidationError(
            f"Catalog entry fields are invalid: {path.relative_to(ROOT)}"
        )
    manifest = _validate_manifest(raw["manifest"])
    package_url = _validate_release_url(
        raw["package_url"], origin=origin, manifest=manifest, path=path
    )
    digest = raw["sha256"]
    size = raw["package_size"]
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise ValidationError("Catalog SHA-256 must be lowercase hex")
    if type(size) is not int or not 1 <= size <= MAX_ARCHIVE_BYTES:
        raise ValidationError("Catalog package size is outside the limit")
    if verify_assets:
        request = urllib.request.Request(
            package_url,
            headers={"User-Agent": "PupuDanmu-PluginRegistry/1"},
        )
        opener = urllib.request.build_opener(_SafeReleaseRedirect())
        try:
            with opener.open(request, timeout=30.0) as response:
                payload = response.read(MAX_ARCHIVE_BYTES + 1)
        except (OSError, urllib.error.URLError, ValidationError):
            raise ValidationError("could not fetch approved Release asset") from None
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
            raise ValidationError(
                "Release asset size or SHA-256 does not match the entry"
            )
        _validate_archive(payload, manifest)
    return {
        "origin": origin,
        "manifest": manifest,
        "package_url": package_url,
        "sha256": digest,
        "package_size": size,
    }


def _collect_entries(*, verify_assets: bool) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for path in CATALOG_ROOT.rglob("*"):
        if path.is_symlink():
            raise ValidationError("Catalog paths cannot be symlinks")
        if (
            path.is_file()
            and path.suffix.lower() != ".json"
            and path.name != ".gitkeep"
        ):
            raise ValidationError("registry Catalog may contain metadata JSON only")
    for origin in ("official", "community"):
        directory = CATALOG_ROOT / origin
        for path in sorted(directory.rglob("*.json")):
            if path.is_symlink():
                raise ValidationError("Catalog entries cannot be symlinks")
            entry = _entry(path, origin, verify_assets=verify_assets)
            manifest = entry["manifest"]
            assert isinstance(manifest, dict)
            plugin_id = manifest["id"]
            assert isinstance(plugin_id, str)
            if plugin_id in seen_ids:
                raise ValidationError("duplicate plugin id across Catalog entries")
            seen_ids.add(plugin_id)
            if origin == "official" and path.parent != directory:
                raise ValidationError(
                    "official entries must be directly under catalog/official"
                )
            if origin == "community":
                if (
                    len(path.relative_to(directory).parts) != 2
                    or path.suffix.lower() != ".json"
                ):
                    raise ValidationError(
                        "community entry must be catalog/community/<publisher>/<plugin>.json"
                    )
                if path.stem != plugin_id.rsplit(".", 1)[-1]:
                    raise ValidationError(
                        "community entry filename must match its plugin id suffix"
                    )
            entries.append(entry)
    entries.sort(key=lambda item: item["manifest"]["id"])
    return entries


def _load_index(path: Path) -> dict[str, object]:
    value = _read_json(path, maximum_bytes=1024 * 1024)
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "generated_at",
        "plugins",
    }:
        raise ValidationError("generated index fields are invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValidationError("generated index schema version is unsupported")
    timestamp = value["generated_at"]
    if not isinstance(timestamp, str) or len(timestamp) > 40:
        raise ValidationError("generated index timestamp is invalid")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("generated index timestamp is invalid") from None
    if (
        parsed_timestamp.utcoffset() is None
        or parsed_timestamp.utcoffset().total_seconds() != 0
    ):
        raise ValidationError("generated index timestamp must use UTC")
    if not isinstance(value["plugins"], list):
        raise ValidationError("generated index plugins must be a list")
    if len(value["plugins"]) > 256:
        raise ValidationError("generated index contains too many plugins")
    return value


def _generated_index(entries: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "plugins": entries,
    }


def _check_index(entries: list[dict[str, object]]) -> None:
    checked_in = _load_index(INDEX_PATH)
    if checked_in["plugins"] != entries:
        raise ValidationError(
            "v1/index.json is stale; regenerate it from Catalog entries"
        )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _build_site(directory: Path, entries: list[dict[str, object]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(directory / "v1" / "index.json", _generated_index(entries))
    schema_directory = directory / "schemas"
    schema_directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        ROOT / "schemas" / "manifest.schema.json",
        schema_directory / "manifest.schema.json",
    )
    shutil.copyfile(
        ROOT / "schemas" / "catalog-entry.schema.json",
        schema_directory / "catalog-entry.schema.json",
    )
    (directory / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pupu Danmu Plugin Registry</title><main><h1>Pupu Danmu Plugin Registry</h1><p>This site serves Catalog metadata only. Plugin source and ZIP packages are hosted by their source repositories.</p><p><a href="v1/index.json">Catalog index v1</a></p></main></html>\n',
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate source Catalog and checked-in index",
    )
    parser.add_argument(
        "--verify-assets",
        action="store_true",
        help="download bounded static ZIPs and verify SHA-256 and layout",
    )
    parser.add_argument(
        "--write-index",
        action="store_true",
        help="regenerate the checked-in v1/index.json",
    )
    parser.add_argument(
        "--site-dir", type=Path, help="build a GitHub Pages site directory"
    )
    args = parser.parse_args(argv)
    try:
        entries = _collect_entries(verify_assets=args.verify_assets)
        if args.check:
            _check_index(entries)
        if args.write_index:
            _write_json(INDEX_PATH, _generated_index(entries))
        if args.site_dir is not None:
            _build_site(args.site_dir, entries)
    except (
        OSError,
        ValidationError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
    ) as error:
        print(f"registry validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
