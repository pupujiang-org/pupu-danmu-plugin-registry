"""Official source ownership and license policy checks for plugin Catalog entries."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_index

VALID_ENTRY_MANIFEST: dict[str, object] = {
    "schema_version": 1,
    "name": "Sample Plugin",
    "version": "1.0.0",
    "description": "A sample data-only plugin.",
    "author": "community-author",
    "license": "MIT",
    "min_host_version": "0.5.0b3",
    "kind": "ai-participation",
    "runtime_type": "declarative",
    "plugin_api_version": 1,
    "capabilities": ["persona"],
    "config_schema": {},
    "recommended_pacing": {},
    "resources": {},
}
PLUGIN_VERSION = "1.0.0"
OFFICIAL_PLUGIN_ID = "org.pupujiang.fabric-buyer"
COMMUNITY_PLUGIN_ID = "org.community-author.sample-plugin"
COMMUNITY_PUBLISHER = "community-author"
OFFICIAL_MIT_REJECTION = "official Catalog entries must declare the MIT license"


class CatalogEntryLicenseTestCase(unittest.TestCase):
    """Run the real entry validator against a throwaway Catalog entry."""

    def _validate_entry(
        self, *, origin: str, plugin_id: str, license_expression: str
    ) -> dict[str, object]:
        slug = plugin_id.rsplit(".", 1)[-1]
        manifest = dict(
            VALID_ENTRY_MANIFEST,
            id=plugin_id,
            version=PLUGIN_VERSION,
            license=license_expression,
        )
        if origin == "official":
            owner = "pupujiang-org"
            repository = build_index.OFFICIAL_PLUGIN_RELEASE_REPOSITORIES[plugin_id]
            relative = Path("official") / f"{slug}.json"
        else:
            owner = COMMUNITY_PUBLISHER
            repository = f"pupu-danmu-plugin-{slug}"
            relative = Path("community") / COMMUNITY_PUBLISHER / f"{slug}.json"
        payload = {
            "manifest": manifest,
            "package_url": (
                f"https://github.com/{owner}/{repository}/releases/download/"
                f"{slug}-v{PLUGIN_VERSION}/{slug}-{PLUGIN_VERSION}.zip"
            ),
            "sha256": "0" * 64,
            "package_size": 1024,
        }
        with tempfile.TemporaryDirectory() as temporary_dir:
            catalog_root = Path(temporary_dir)
            path = catalog_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(build_index, "CATALOG_ROOT", catalog_root):
                return build_index._entry(path, origin, verify_assets=False)


class DeclaredLicenseExpressionTests(unittest.TestCase):
    def test_pinned_spdx_expressions_are_accepted(self) -> None:
        licenses, exceptions = build_index._spdx_identifiers()
        self.assertIn("mit", licenses)
        self.assertIn("apache-2.0", licenses)
        self.assertIn("classpath-exception-2.0", exceptions)
        for expression in (
            "MIT",
            "Apache-2.0",
            "Apache-2.0 OR MIT",
            "(Apache-2.0 AND MIT) OR MIT",
            "GPL-2.0-or-later WITH Classpath-exception-2.0",
            "(MIT)",
            # LicenseRef identifiers are accepted by pattern, not by the pinned list.
            "LicenseRef-Company-Data",
        ):
            with self.subTest(expression=expression):
                build_index._validate_declared_license(expression)

    def test_pinned_spdx_data_file_is_the_validation_source(self) -> None:
        data = json.loads(build_index.SPDX_DATA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["license_list_version"], "3.29.0")
        for field in ("license_ids", "exception_ids"):
            with self.subTest(field=field):
                identifiers = data[field]
                self.assertTrue(identifiers)
                folded = [item.casefold() for item in identifiers]
                self.assertEqual(len(folded), len(set(folded)))

    def test_identifiers_outside_the_pinned_license_list_are_rejected(self) -> None:
        licenses, exceptions = build_index._spdx_identifiers()
        self.assertIn("classpath-exception-2.0", exceptions)
        self.assertNotIn("classpath-exception-2.0", licenses)
        for identifier in (
            # A pinned SPDX exception id is not a declared license id.
            "Classpath-exception-2.0",
            "Apache-2.1",
            "Unknown-License-1.0",
        ):
            with (
                self.subTest(identifier=identifier),
                self.assertRaisesRegex(
                    build_index.ValidationError, "unknown SPDX identifier"
                ),
            ):
                build_index._validate_declared_license(identifier)

    def test_unknown_spdx_exception_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            build_index.ValidationError, "unknown SPDX exception"
        ):
            build_index._validate_declared_license(
                "GPL-2.0-or-later WITH Classpath-exception-9.9"
            )

    def test_malformed_expressions_are_rejected(self) -> None:
        for expression in (
            "MIT OR",
            "MIT AND",
            "MIT WITH",
            "Apache 2.0",
            "(MIT",
            "MIT)",
            ") MIT",
            "()",
        ):
            with (
                self.subTest(expression=expression),
                self.assertRaises(build_index.ValidationError),
            ):
                build_index._validate_declared_license(expression)

    def test_placeholder_or_empty_licenses_are_rejected(self) -> None:
        for expression in (
            "",
            "   ",
            "CHOOSE_LICENSE",
            "TODO",
            "NONE",
            "NOASSERTION",
            "none",
            "noassertion",
        ):
            with (
                self.subTest(expression=expression),
                self.assertRaises(build_index.ValidationError),
            ):
                build_index._validate_declared_license(expression)


class OfficialCatalogLicenseTests(CatalogEntryLicenseTestCase):
    def test_official_entries_accept_a_declared_mit_license(self) -> None:
        entry = self._validate_entry(
            origin="official", plugin_id=OFFICIAL_PLUGIN_ID, license_expression="MIT"
        )
        self.assertEqual(entry["origin"], "official")
        self.assertEqual(entry["manifest"]["license"], "MIT")

    def test_official_entries_reject_any_other_license_declaration(self) -> None:
        for expression in (
            "Apache-2.0",
            "Apache-2.0 OR MIT",
            "MIT OR Apache-2.0",
            # The official rule compares the declaration literally, so "mit" is not MIT.
            "mit",
        ):
            with (
                self.subTest(expression=expression),
                self.assertRaisesRegex(
                    build_index.ValidationError, OFFICIAL_MIT_REJECTION
                ),
            ):
                self._validate_entry(
                    origin="official",
                    plugin_id=OFFICIAL_PLUGIN_ID,
                    license_expression=expression,
                )

    def test_checked_in_official_entries_declare_mit(self) -> None:
        official = [
            entry
            for entry in build_index._collect_entries(verify_assets=False)
            if entry["origin"] == "official"
        ]
        self.assertTrue(official)
        for entry in official:
            manifest = entry["manifest"]
            with self.subTest(plugin_id=manifest["id"]):
                self.assertEqual(manifest["license"], "MIT")


class CommunityCatalogLicenseTests(CatalogEntryLicenseTestCase):
    def test_community_entries_accept_non_mit_pinned_spdx_licenses(self) -> None:
        for expression in (
            "Apache-2.0",
            "Apache-2.0 OR MIT",
            "GPL-2.0-or-later WITH Classpath-exception-2.0",
        ):
            with self.subTest(expression=expression):
                entry = self._validate_entry(
                    origin="community",
                    plugin_id=COMMUNITY_PLUGIN_ID,
                    license_expression=expression,
                )
                self.assertEqual(entry["origin"], "community")
                self.assertEqual(entry["manifest"]["license"], expression)

    def test_community_entries_reject_invalid_license_declarations(self) -> None:
        for expression in (
            "",
            "   ",
            "Unknown-License-1.0",
            "MIT OR",
            "NONE",
            "Classpath-exception-2.0",
        ):
            with (
                self.subTest(expression=expression),
                self.assertRaises(build_index.ValidationError),
            ):
                self._validate_entry(
                    origin="community",
                    plugin_id=COMMUNITY_PLUGIN_ID,
                    license_expression=expression,
                )


class OfficialReleaseRepositoryTests(unittest.TestCase):
    def _validate(self, plugin_id: str, repository: str) -> str:
        slug = plugin_id.rsplit(".", 1)[-1]
        manifest = {"id": plugin_id, "version": "1.0.0"}
        path = build_index.CATALOG_ROOT / "official" / f"{slug}.json"
        url = (
            f"https://github.com/pupujiang-org/{repository}/releases/download/"
            f"{slug}-v1.0.0/{slug}-1.0.0.zip"
        )
        return build_index._validate_release_url(
            url, origin="official", manifest=manifest, path=path
        )

    def test_fabric_buyer_uses_its_dedicated_official_repository(self) -> None:
        url = self._validate(
            "org.pupujiang.fabric-buyer", "pupu-danmu-plugin-fabric-buyer"
        )
        self.assertIn("pupu-danmu-plugin-fabric-buyer", url)

    def test_music_audience_uses_its_dedicated_official_repository(self) -> None:
        url = self._validate(
            "org.pupujiang.music-audience", "pupu-danmu-plugin-music-audience"
        )
        self.assertIn("pupu-danmu-plugin-music-audience", url)

    def test_official_plugins_reject_cross_repo_and_legacy_repo_urls(self) -> None:
        for repository in (
            "pupu-danmu-plugin-music-audience",
            "pupu-danmu-plugins",
            "unreviewed-plugin",
        ):
            with self.subTest(repository=repository):
                with self.assertRaises(build_index.ValidationError):
                    self._validate("org.pupujiang.fabric-buyer", repository)

    def test_unmapped_official_plugin_is_rejected(self) -> None:
        with self.assertRaises(build_index.ValidationError):
            self._validate(
                "org.pupujiang.unreviewed-plugin",
                "pupu-danmu-plugin-unreviewed-plugin",
            )

    def test_official_plugin_id_cannot_be_claimed_under_community_origin(self) -> None:
        plugin_id = "org.pupujiang.fabric-buyer"
        slug = plugin_id.rsplit(".", 1)[-1]
        manifest = {"id": plugin_id, "version": "1.0.0"}
        path = (
            build_index.CATALOG_ROOT / "community" / "community-author" / f"{slug}.json"
        )
        url = (
            "https://github.com/community-author/fabric-buyer/releases/download/"
            "fabric-buyer-v1.0.0/fabric-buyer-1.0.0.zip"
        )

        with self.assertRaises(build_index.ValidationError):
            build_index._validate_release_url(
                url, origin="community", manifest=manifest, path=path
            )

    def test_catalog_contains_both_published_official_releases(self) -> None:
        entries = build_index._collect_entries(verify_assets=False)
        official = {
            entry["manifest"]["id"]: entry
            for entry in entries
            if entry["origin"] == "official"
        }
        self.assertEqual(
            set(official),
            {"org.pupujiang.fabric-buyer", "org.pupujiang.music-audience"},
        )
        for plugin_id, repository in (
            ("org.pupujiang.fabric-buyer", "pupu-danmu-plugin-fabric-buyer"),
            ("org.pupujiang.music-audience", "pupu-danmu-plugin-music-audience"),
        ):
            with self.subTest(plugin_id=plugin_id):
                entry = official[plugin_id]
                self.assertIn(repository, entry["package_url"])
                self.assertRegex(entry["sha256"], build_index.SHA256)
                self.assertGreater(entry["package_size"], 0)

    def test_generated_index_exposes_official_and_community_origins(self) -> None:
        index = build_index._load_index(build_index.INDEX_PATH)
        entries = index["plugins"]
        self.assertEqual(
            {entry["origin"] for entry in entries}, {"official", "community"}
        )
        self.assertEqual(
            {entry["manifest"]["id"] for entry in entries},
            {
                "org.pupujiang.fabric-buyer",
                "org.pupujiang.music-audience",
                "org.wtwang1998.community-demo",
            },
        )


if __name__ == "__main__":
    unittest.main()
