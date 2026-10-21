"""Validate registry metadata and build a Pages index without executing packages."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import math
import re
import shutil
import stat
import sys
import urllib.error
import urllib.request
import zipfile
import zlib
from datetime import UTC
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
CATALOG_ROOT = ROOT / "catalog"
INDEX_PATH = ROOT / "v1" / "index.json"
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
MAX_WORKER_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_WORKER_FILE_BYTES = 1024 * 1024
MAX_PROMPT_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 5 * 1024 * 1024
MAX_WORKER_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILES = 100
MAX_WORKER_FILES = 2000
MAX_RATIO = 100
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PLUGIN_ID = re.compile(r"^(?:[a-z][a-z0-9-]*\.){2,}[a-z0-9][a-z0-9-]*$")
SEMVER_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    rf"(?:-({SEMVER_IDENTIFIER}(?:\.{SEMVER_IDENTIFIER})*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
HOST_VERSION = re.compile(
    r"^(?:[0-9]+!)?(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))*"
    r"(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?"
    r"(?:\+[a-z0-9]+(?:\.[a-z0-9]+)*)?$"
)
MIT_LICENSE = "MIT"
SPDX_DATA_PATH = Path(__file__).resolve().with_name("spdx-license-ids.json")
SPDX_TOKEN = re.compile(r"[()]|[^\s()]+")
SPDX_LICENSE_REF = re.compile(
    r"^(?:DocumentRef-[A-Za-z0-9.-]+:)?LicenseRef-[A-Za-z0-9.-]+$"
)
SPDX_OPERATORS = {"and", "or"}
SPDX_WITH = "with"
SPDX_UNKNOWN = {"none", "noassertion"}
SPDX_OPERATOR_TOKENS = SPDX_OPERATORS | {SPDX_WITH}
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
WORKER_ROOT_FILES = {"manifest.json", "README.md", "LICENSE"}
WORKER_CAPABILITIES = {
    "chat.read",
    "room.read",
    "session.config.read",
    "llm.generate",
    "storage.session",
}
WORKER_TOP_LEVEL_FIELDS = TOP_LEVEL_MANIFEST_FIELDS | {"entrypoint"}
WORKER_ENTRYPOINT = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$"
)
WORKER_PACKAGE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")
ALLOWED_REDIRECT_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}
OFFICIAL_PLUGIN_RELEASE_REPOSITORIES = {
    "org.pupujiang.fabric-buyer": "pupu-danmu-plugin-fabric-buyer",
    "org.pupujiang.music-audience": "pupu-danmu-plugin-music-audience",
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


@lru_cache(maxsize=1)
def _spdx_identifiers() -> tuple[dict[str, str], dict[str, str]]:
    """Casefolded known SPDX license and exception identifiers."""
    try:
        payload = SPDX_DATA_PATH.read_bytes()
    except OSError:
        raise ValidationError("the vendored SPDX identifier list is missing") from None
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValidationError("the vendored SPDX identifier list is invalid") from None
    if not isinstance(data, dict):
        raise ValidationError("the vendored SPDX identifier list is invalid")
    licenses = data.get("license_ids")
    exceptions = data.get("exception_ids")
    if (
        not isinstance(licenses, list)
        or not isinstance(exceptions, list)
        or not licenses
        or not all(isinstance(item, str) and item for item in licenses)
        or not all(isinstance(item, str) and item for item in exceptions)
    ):
        raise ValidationError("the vendored SPDX identifier list is invalid")
    return (
        {item.casefold(): item for item in licenses},
        {item.casefold(): item for item in exceptions},
    )


def _validate_declared_license(value: str) -> None:
    """Require a resolvable SPDX license expression; NONE/NOASSERTION are not a license."""
    licenses, exceptions = _spdx_identifiers()
    tokens = SPDX_TOKEN.findall(value)
    if not tokens:
        raise ValidationError("Manifest license is empty")
    position = 0

    def take() -> str | None:
        nonlocal position
        if position >= len(tokens):
            return None
        token = tokens[position]
        position += 1
        return token

    def parse_simple() -> bool:
        token = take()
        if token is None:
            raise ValidationError("Manifest license is incomplete")
        if token == "(":
            parse_expression()
            if take() != ")":
                raise ValidationError("Manifest license has unbalanced parentheses")
            return False
        if token == ")" or token.casefold() in SPDX_OPERATOR_TOKENS:
            raise ValidationError("Manifest license is malformed")
        identifier = token[:-1] if token.endswith("+") and token != "+" else token
        if identifier.casefold() in SPDX_UNKNOWN:
            raise ValidationError(
                "Manifest license must be a declared SPDX license, not NONE or NOASSERTION"
            )
        if SPDX_LICENSE_REF.fullmatch(identifier) is None and (
            identifier.casefold() not in licenses
        ):
            raise ValidationError(
                f"Manifest license has an unknown SPDX identifier: {identifier}"
            )
        return True

    def parse_term() -> None:
        nonlocal position
        simple = parse_simple()
        if position < len(tokens) and tokens[position].casefold() == SPDX_WITH:
            if not simple:
                raise ValidationError(
                    "Manifest license WITH must follow one license identifier"
                )
            position += 1
            exception = take()
            if (
                exception is None
                or exception == ")"
                or exception.casefold() in SPDX_OPERATOR_TOKENS
            ):
                raise ValidationError("Manifest license WITH is incomplete")
            if (
                SPDX_LICENSE_REF.fullmatch(exception) is None
                and exception.casefold() not in exceptions
            ):
                raise ValidationError(
                    f"Manifest license has an unknown SPDX exception: {exception}"
                )

    def parse_expression() -> None:
        nonlocal position
        parse_term()
        while position < len(tokens) and tokens[position].casefold() in SPDX_OPERATORS:
            position += 1
            parse_term()

    parse_expression()
    if position != len(tokens):
        raise ValidationError("Manifest license has trailing content")


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
    _validate_declared_license(value["license"])
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


def _check_worker_json_tree(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > 4096 or depth > 16:
            raise ValidationError("Worker JSON exceeds its nesting or node limit")
        if isinstance(current, dict):
            if any(
                not isinstance(key, str) or len(key) > 128 for key in current
            ):
                raise ValidationError("Worker JSON object key is invalid")
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
        elif isinstance(current, str):
            if len(current) > MAX_WORKER_FILE_BYTES or "\x00" in current:
                raise ValidationError("Worker JSON text is invalid")
        elif isinstance(current, float) and not math.isfinite(current):
            raise ValidationError("Worker JSON contains a non-finite number")
        elif current is not None and type(current) not in {int, bool}:
            raise ValidationError("Worker JSON contains an unsupported value")


def _validate_worker_config_schema(value: object) -> None:
    if not isinstance(value, dict) or len(value) > 32:
        raise ValidationError("Worker config schema is invalid")
    field_id_pattern = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
    allowed_fields = {
        "type",
        "label",
        "description",
        "required",
        "default",
        "options",
        "minimum",
        "maximum",
        "min_length",
        "max_length",
    }
    for field_id, field in value.items():
        if (
            not isinstance(field_id, str)
            or field_id_pattern.fullmatch(field_id) is None
            or not isinstance(field, dict)
            or not {"type", "label"}.issubset(field)
            or not set(field).issubset(allowed_fields)
        ):
            raise ValidationError("Worker config field is invalid")
        kind = field["type"]
        label = field["label"]
        description = field.get("description", "")
        if (
            not isinstance(kind, str)
            or kind not in {"text", "select", "checkbox", "number"}
            or not isinstance(label, str)
            or not label.strip()
            or len(label) > 80
            or not isinstance(description, str)
            or len(description) > 300
            or type(field.get("required", False)) is not bool
        ):
            raise ValidationError("Worker config field metadata is invalid")
        if kind == "text":
            if set(field) - {
                "type",
                "label",
                "description",
                "required",
                "default",
                "min_length",
                "max_length",
            }:
                raise ValidationError("Worker text config contains unsupported fields")
            minimum = field.get("min_length", 0)
            maximum = field.get("max_length", 500)
            default = field.get("default")
            if (
                type(minimum) is not int
                or not 0 <= minimum <= 500
                or type(maximum) is not int
                or not 1 <= maximum <= 500
                or minimum > maximum
                or (
                    default is not None
                    and (
                        not isinstance(default, str)
                        or not minimum <= len(default) <= maximum
                    )
                )
            ):
                raise ValidationError("Worker text config bounds are invalid")
        elif kind == "select":
            options = field.get("options")
            if (
                set(field)
                - {"type", "label", "description", "required", "default", "options"}
                or not isinstance(options, list)
                or not 1 <= len(options) <= 32
                or not all(
                    isinstance(option, str) and option.strip() and len(option) <= 80
                    for option in options
                )
                or len(set(options)) != len(options)
                or (
                    field.get("default") is not None
                    and field["default"] not in options
                )
            ):
                raise ValidationError("Worker select config options are invalid")
        elif kind == "checkbox":
            if set(field) - {
                "type",
                "label",
                "description",
                "required",
                "default",
            } or (
                field.get("default") is not None
                and type(field["default"]) is not bool
            ):
                raise ValidationError("Worker checkbox config is invalid")
        else:
            minimum = field.get("minimum")
            maximum = field.get("maximum")
            default = field.get("default")
            if (
                set(field)
                - {"type", "label", "description", "required", "default", "minimum", "maximum"}
                or type(minimum) not in {int, float}
                or type(maximum) not in {int, float}
                or not math.isfinite(minimum)
                or not math.isfinite(maximum)
                or minimum >= maximum
                or maximum - minimum > 1_000_000
                or (
                    default is not None
                    and (
                        type(default) not in {int, float}
                        or not math.isfinite(default)
                        or not minimum <= default <= maximum
                    )
                )
            ):
                raise ValidationError("Worker number config bounds are invalid")


def _validate_worker_pacing(value: object) -> None:
    if not isinstance(value, dict) or not set(value).issubset(
        {"min_interval_seconds", "max_interval_seconds", "hourly_limit"}
    ):
        raise ValidationError("Worker recommended pacing is invalid")
    bounds = {
        "min_interval_seconds": (1, 3600),
        "max_interval_seconds": (1, 3600),
        "hourly_limit": (1, 240),
    }
    for field, (minimum, maximum) in bounds.items():
        if field in value and (
            type(value[field]) is not int or not minimum <= value[field] <= maximum
        ):
            raise ValidationError("Worker recommended pacing bound is invalid")
    if (
        "min_interval_seconds" in value
        and "max_interval_seconds" in value
        and value["min_interval_seconds"] > value["max_interval_seconds"]
    ):
        raise ValidationError("Worker recommended pacing bounds are inconsistent")


def _validate_worker_resources(value: object) -> None:
    if not isinstance(value, dict) or len(value) > 4:
        raise ValidationError("Worker resources are invalid")
    allowed = {
        "personas": {"personas.json"},
        "topics": {"topics.json"},
        "participation": {"participation.json"},
    }
    for kind, path in value.items():
        if not isinstance(kind, str) or not isinstance(path, str):
            raise ValidationError("Worker resource references must be text")
        if kind == "prompt":
            valid = (
                path.startswith("prompts/")
                and path.endswith((".txt", ".md"))
                and path.count("/") == 1
                and not path.endswith("/")
            )
        else:
            valid = kind in allowed and path in allowed[kind]
        if not valid:
            raise ValidationError("Worker resource reference is unsupported")


def _validate_worker_manifest(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != WORKER_TOP_LEVEL_FIELDS:
        raise ValidationError("Worker Manifest fields are missing or unsupported")
    _check_worker_json_tree(value)
    if type(value["schema_version"]) is not int or value["schema_version"] != 2:
        raise ValidationError("unsupported Worker Manifest schema version")
    plugin_id = value["id"]
    version = value["version"]
    if (
        not isinstance(plugin_id, str)
        or len(plugin_id) > 128
        or PLUGIN_ID.fullmatch(plugin_id) is None
    ):
        raise ValidationError("Worker plugin id is invalid")
    if (
        not isinstance(version, str)
        or len(version) > 80
        or SEMVER.fullmatch(version) is None
    ):
        raise ValidationError("Worker plugin version is invalid")
    for field, maximum in (
        ("name", 100),
        ("description", 500),
        ("author", 100),
        ("license", 100),
        ("min_host_version", 80),
    ):
        text = value[field]
        if (
            not isinstance(text, str)
            or not text.strip()
            or "\x00" in text
            or len(text) > maximum
        ):
            raise ValidationError(f"Worker Manifest {field} is invalid")
    if HOST_VERSION.fullmatch(value["min_host_version"]) is None:
        raise ValidationError("Worker minimum host version is invalid")
    _validate_declared_license(value["license"])
    if value["kind"] != "ai-participation":
        raise ValidationError("unsupported Worker kind")
    if value["runtime_type"] != "python-worker":
        raise ValidationError("unsupported Worker runtime")
    if type(value["plugin_api_version"]) is not int or value["plugin_api_version"] != 2:
        raise ValidationError("unsupported Worker plugin API version")
    capabilities = value["capabilities"]
    if (
        not isinstance(capabilities, list)
        or len(capabilities) > len(WORKER_CAPABILITIES)
        or not all(isinstance(item, str) and len(item) <= 64 for item in capabilities)
        or len(set(capabilities)) != len(capabilities)
        or not set(capabilities).issubset(WORKER_CAPABILITIES)
    ):
        raise ValidationError("Worker capabilities are invalid")
    entrypoint = value["entrypoint"]
    if (
        not isinstance(entrypoint, str)
        or len(entrypoint) > 160
        or WORKER_ENTRYPOINT.fullmatch(entrypoint) is None
    ):
        raise ValidationError("Worker entrypoint is invalid")
    _validate_worker_config_schema(value["config_schema"])
    _validate_worker_pacing(value["recommended_pacing"])
    _validate_worker_resources(value["resources"])
    return value


def _read_worker_json(payload: bytes) -> object:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValidationError("Worker JSON contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValidationError("Worker JSON contains a non-finite number")
            ),
        )
    except ValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise ValidationError("Worker JSON is invalid UTF-8 JSON") from None
    _check_worker_json_tree(value)
    return value


def _is_worker_static_resource(path: str) -> bool:
    if path in {"personas.json", "topics.json", "participation.json"}:
        return True
    return (
        path.startswith("prompts/")
        and path.count("/") == 1
        and path.endswith((".txt", ".md"))
    )


def _validate_worker_archive(
    payload: bytes, manifest: dict[str, object]
) -> dict[str, object]:
    if not isinstance(payload, bytes) or not payload:
        raise ValidationError("Worker package archive is empty")
    if len(payload) > MAX_WORKER_ARCHIVE_BYTES:
        raise ValidationError("Worker package archive exceeds the size limit")
    manifest = _validate_worker_manifest(manifest)

    files: dict[str, bytes] = {}
    seen: set[str] = set()
    directories: set[str] = set()
    total_size = 0
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= MAX_WORKER_FILES:
                raise ValidationError("Worker package has an invalid file count")
            for info in entries:
                name = info.filename
                is_directory = info.is_dir()
                canonical = name[:-1] if is_directory else name
                parts = canonical.split("/")
                if (
                    not canonical
                    or WORKER_PACKAGE_PATH.fullmatch(name) is None
                    or "\\" in name
                    or ":" in name
                    or name.startswith("/")
                    or PurePosixPath(canonical).as_posix() != canonical
                    or any(part in {"", ".", ".."} for part in parts)
                    or any(part.startswith(".") for part in parts)
                ):
                    raise ValidationError("Worker package contains an unsafe path")
                folded = canonical.casefold()
                if folded in seen:
                    raise ValidationError("Worker package contains a duplicate path")
                seen.add(folded)

                mode = info.external_attr >> 16
                mode_type = stat.S_IFMT(mode)
                if (
                    stat.S_ISLNK(mode)
                    or info.flag_bits & 0x1
                    or mode_type not in {0, stat.S_IFREG, stat.S_IFDIR}
                ):
                    raise ValidationError("Worker package contains a link or special file")
                if is_directory:
                    if (
                        mode_type not in {0, stat.S_IFDIR}
                        or info.file_size != 0
                        or info.compress_size != 0
                    ):
                        raise ValidationError("Worker package contains an invalid directory entry")
                    if (
                        canonical != "worker"
                        and not canonical.startswith("worker/")
                        and canonical != "prompts"
                    ):
                        raise ValidationError("Worker package contains an unsupported directory")
                    directories.add(canonical)
                    continue
                if mode_type == stat.S_IFDIR:
                    raise ValidationError("Worker package contains an invalid directory entry")

                is_root_file = canonical in WORKER_ROOT_FILES
                is_worker_source = canonical.startswith("worker/") and canonical.endswith(".py")
                is_static_resource = _is_worker_static_resource(canonical)
                if not (is_root_file or is_worker_source or is_static_resource):
                    raise ValidationError("Worker package contains an unsupported file")
                if is_worker_source:
                    source_parts = parts[1:]
                    if not source_parts or any(
                        part != "__init__.py"
                        and not part.removesuffix(".py").isidentifier()
                        for part in source_parts
                    ):
                        raise ValidationError("Worker source path is invalid")
                if info.file_size > MAX_WORKER_FILE_BYTES:
                    raise ValidationError("Worker package file exceeds the size limit")
                total_size += info.file_size
                if total_size > MAX_WORKER_TOTAL_BYTES:
                    raise ValidationError("Worker package expands beyond the size limit")
                if info.file_size and (
                    info.compress_size == 0
                    or info.file_size > info.compress_size * MAX_RATIO
                ):
                    raise ValidationError("Worker package compression ratio is excessive")
                with archive.open(info) as source:
                    content = source.read(MAX_WORKER_FILE_BYTES + 1)
                if len(content) != info.file_size or len(content) > MAX_WORKER_FILE_BYTES:
                    raise ValidationError("Worker package entry size is inconsistent")
                if canonical == "manifest.json" and len(content) > 64 * 1024:
                    raise ValidationError("Worker manifest exceeds the size limit")
                if is_worker_source:
                    try:
                        ast.parse(content.decode("utf-8"), filename=canonical)
                    except (UnicodeDecodeError, SyntaxError, ValueError, RecursionError):
                        raise ValidationError("Worker source is not valid UTF-8 Python") from None
                files[canonical] = content
    except ValidationError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError, zlib.error):
        raise ValidationError("Worker package archive is invalid") from None

    manifest_content = files.get("manifest.json")
    if manifest_content is None:
        raise ValidationError("Worker package has no manifest")
    package_manifest = _validate_worker_manifest(_read_worker_json(manifest_content))
    if package_manifest != manifest:
        raise ValidationError("Worker package Manifest differs from Catalog entry")

    resources = manifest["resources"]
    assert isinstance(resources, dict)
    resource_paths = set(resources.values())
    packaged_resources = {path for path in files if _is_worker_static_resource(path)}
    if packaged_resources != resource_paths:
        raise ValidationError("Worker package resources do not match the manifest")
    if "prompts" in directories and not any(
        path.startswith("prompts/") for path in resource_paths
    ):
        raise ValidationError("Worker package contains an unused prompt directory")
    for resource_path in resource_paths:
        content = files[resource_path]
        if resource_path.startswith("prompts/"):
            if len(content) > MAX_PROMPT_BYTES:
                raise ValidationError("Worker prompt resource exceeds the size limit")
            try:
                prompt = content.decode("utf-8")
            except UnicodeDecodeError:
                raise ValidationError("Worker prompt resource is not UTF-8") from None
            if "\x00" in prompt:
                raise ValidationError("Worker prompt resource contains invalid text")
        else:
            resource = _read_worker_json(content)
            if not isinstance(resource, dict):
                raise ValidationError("Worker JSON resource root must be an object")

    module, _function = manifest["entrypoint"].split(":", maxsplit=1)
    module_path = PurePosixPath("worker", *module.split("."))
    if (
        module_path.with_suffix(".py").as_posix() not in files
        and (module_path / "__init__.py").as_posix() not in files
    ):
        raise ValidationError("Worker package entrypoint is missing")
    return manifest


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
        official_repository = OFFICIAL_PLUGIN_RELEASE_REPOSITORIES.get(plugin_id)
        if (
            relative[0] != "official"
            or owner != "pupujiang-org"
            or official_repository is None
            or repository != official_repository
        ):
            raise ValidationError(
                "official entry must use the official plugin Release source"
            )
    else:
        if plugin_id in OFFICIAL_PLUGIN_RELEASE_REPOSITORIES:
            raise ValidationError(
                "official plugin id cannot be submitted as a community entry"
            )
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


def _validate_worker_release_url(
    url: object, *, origin: str, manifest: dict[str, object], path: Path
) -> str:
    if not isinstance(url, str):
        raise ValidationError("Worker package URL must be text")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError("Worker package URL must be a fixed HTTPS GitHub Release asset")
    parts = parsed.path.split("/")
    if len(parts) != 7 or parts[0] != "" or parts[3:5] != ["releases", "download"]:
        raise ValidationError("Worker package URL path is invalid")
    owner, repository, tag, filename = parts[1], parts[2], parts[5], parts[6]
    plugin_id = manifest["id"]
    version = manifest["version"]
    assert isinstance(plugin_id, str) and isinstance(version, str)
    slug = plugin_id.rsplit(".", 1)[-1]
    if (
        not re.fullmatch(r"[A-Za-z0-9-]{1,100}", owner)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository)
    ):
        raise ValidationError("Worker Release owner or repository is invalid")
    if path.stem != slug:
        raise ValidationError("Worker Catalog filename must match the manifest id suffix")
    if tag != f"{slug}-v{version}" or filename != f"{slug}-{version}.zip":
        raise ValidationError("Worker Release URL does not match id and version")
    relative = path.relative_to(CATALOG_ROOT).parts
    if origin == "official":
        namespace_parts = plugin_id.split(".")
        if (
            len(relative) != 3
            or relative[:2] != ("workers", "official")
            or len(namespace_parts) != 3
            or namespace_parts[:2] != ["org", "pupujiang"]
            or owner != "pupujiang-org"
            or repository != f"pupu-danmu-plugin-{slug}"
        ):
            raise ValidationError("official Worker must use the official publisher and repository")
    elif origin == "community":
        if (
            len(relative) != 4
            or relative[:2] != ("workers", "community")
            or plugin_id.startswith("org.pupujiang.")
            or owner.casefold() != relative[2].casefold()
            or owner.casefold() == "pupujiang-org"
        ):
            raise ValidationError("community Worker publisher must match its Release owner")
    else:
        raise ValidationError("Worker Catalog origin is invalid")
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
    if origin == "official" and manifest["license"] != MIT_LICENSE:
        raise ValidationError(
            f"official Catalog entries must declare the {MIT_LICENSE} license"
        )
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


def _verify_worker_asset(
    payload: bytes,
    *,
    manifest: dict[str, object],
    expected_size: int,
    expected_sha256: str,
) -> None:
    if len(payload) != expected_size:
        raise ValidationError("Worker Release asset size does not match the entry")
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValidationError("Worker Release asset SHA-256 does not match the entry")
    _validate_worker_archive(payload, manifest)


def _worker_entry(path: Path, origin: str, *, verify_assets: bool) -> dict[str, object]:
    raw = _read_json(path, maximum_bytes=128 * 1024)
    if not isinstance(raw, dict) or set(raw) != {
        "manifest",
        "package_url",
        "sha256",
        "package_size",
    }:
        raise ValidationError(
            f"Worker Catalog entry fields are invalid: {path.relative_to(ROOT)}"
        )
    manifest = _validate_worker_manifest(raw["manifest"])
    if origin == "official" and manifest["license"] != MIT_LICENSE:
        raise ValidationError(
            f"official Worker Catalog entries must declare the {MIT_LICENSE} license"
        )
    package_url = _validate_worker_release_url(
        raw["package_url"], origin=origin, manifest=manifest, path=path
    )
    digest = raw["sha256"]
    size = raw["package_size"]
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise ValidationError("Worker Catalog SHA-256 must be lowercase hex")
    if type(size) is not int or not 1 <= size <= MAX_WORKER_ARCHIVE_BYTES:
        raise ValidationError("Worker package size is outside the limit")
    if verify_assets:
        request = urllib.request.Request(
            package_url,
            headers={"User-Agent": "PupuDanmu-PluginRegistry/1"},
        )
        opener = urllib.request.build_opener(_SafeReleaseRedirect())
        try:
            with opener.open(request, timeout=30.0) as response:
                payload = response.read(MAX_WORKER_ARCHIVE_BYTES + 1)
        except (OSError, urllib.error.URLError, ValidationError):
            raise ValidationError("could not fetch approved Worker Release asset") from None
        if len(payload) > MAX_WORKER_ARCHIVE_BYTES:
            raise ValidationError("Worker Release asset exceeds the size limit")
        _verify_worker_asset(
            payload,
            manifest=manifest,
            expected_size=size,
            expected_sha256=digest,
        )
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


def _collect_worker_entries(*, verify_assets: bool) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    worker_root = CATALOG_ROOT / "workers"
    for origin in ("official", "community"):
        directory = worker_root / origin
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValidationError("Worker Catalog paths cannot be symlinks")
            if not path.is_file() or path.name == ".gitkeep":
                continue
            if path.suffix.lower() != ".json":
                raise ValidationError("Worker Catalog may contain metadata JSON only")
            entry = _worker_entry(path, origin, verify_assets=verify_assets)
            manifest = entry["manifest"]
            assert isinstance(manifest, dict)
            plugin_id = manifest["id"]
            assert isinstance(plugin_id, str)
            if plugin_id in seen_ids:
                raise ValidationError("duplicate Worker id across Catalog entries")
            seen_ids.add(plugin_id)
            relative = path.relative_to(directory).parts
            if origin == "official":
                if len(relative) != 1:
                    raise ValidationError(
                        "official Worker entries must be directly under catalog/workers/official"
                    )
            elif (
                len(relative) != 2
                or path.stem != plugin_id.rsplit(".", 1)[-1]
            ):
                raise ValidationError(
                    "community Worker entry must be catalog/workers/community/<publisher>/<worker>.json"
                )
            entries.append(entry)
    entries.sort(key=lambda item: item["manifest"]["id"])
    return entries


def _check_cross_catalog_conflicts(
    plugins: list[dict[str, object]], workers: list[dict[str, object]]
) -> None:
    seen_releases: set[tuple[str, str]] = set()
    seen_sources: set[str] = set()
    for entries, expected_runtime_type in (
        (plugins, "declarative"),
        (workers, "python-worker"),
    ):
        for entry in entries:
            manifest = entry.get("manifest")
            package_url = entry.get("package_url")
            if (
                not isinstance(manifest, dict)
                or not isinstance(manifest.get("id"), str)
                or not isinstance(manifest.get("version"), str)
            ):
                raise ValidationError("Catalog entry identity is invalid")
            if manifest.get("runtime_type") != expected_runtime_type:
                raise ValidationError("Catalog entry runtime type is invalid")
            release_identity = (manifest["id"], manifest["version"])
            if release_identity in seen_releases:
                raise ValidationError(
                    "duplicate catalog id/version across runtime types"
                )
            seen_releases.add(release_identity)
            if not isinstance(package_url, str):
                raise ValidationError("Catalog package source is invalid")
            if package_url in seen_sources:
                raise ValidationError("duplicate package source across catalog entries")
            seen_sources.add(package_url)


def _load_index(path: Path) -> dict[str, object]:
    value = _read_json(path, maximum_bytes=1024 * 1024)
    required = {"schema_version", "generated_at", "plugins"}
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or set(value) - (required | {"workers"})
    ):
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
    workers = value.get("workers", [])
    if not isinstance(workers, list):
        raise ValidationError("generated index Workers must be a list")
    if len(workers) > 256:
        raise ValidationError("generated index contains too many Workers")
    value["workers"] = workers
    return value


def _generated_index(
    entries: list[dict[str, object]], workers: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "plugins": entries,
        "workers": [] if workers is None else workers,
    }


def _check_index(
    entries: list[dict[str, object]], workers: list[dict[str, object]] | None = None
) -> None:
    checked_in = _load_index(INDEX_PATH)
    if checked_in["plugins"] != entries or checked_in["workers"] != (workers or []):
        raise ValidationError(
            "v1/index.json is stale; regenerate it from Catalog entries"
        )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _build_site(
    directory: Path,
    entries: list[dict[str, object]],
    workers: list[dict[str, object]] | None = None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(directory / "v1" / "index.json", _generated_index(entries, workers))
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
    shutil.copyfile(
        ROOT / "schemas" / "worker-manifest-v2.schema.json",
        schema_directory / "worker-manifest-v2.schema.json",
    )
    shutil.copyfile(
        ROOT / "schemas" / "worker-catalog-entry.schema.json",
        schema_directory / "worker-catalog-entry.schema.json",
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
        workers = _collect_worker_entries(verify_assets=args.verify_assets)
        _check_cross_catalog_conflicts(entries, workers)
        if args.check:
            _check_index(entries, workers)
        if args.write_index:
            _write_json(INDEX_PATH, _generated_index(entries, workers))
        if args.site_dir is not None:
            _build_site(args.site_dir, entries, workers)
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
