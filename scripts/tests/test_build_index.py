"""Official source ownership checks for plugin Catalog Release URLs."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_index


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
