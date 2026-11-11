import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "template_library.py"
spec = importlib.util.spec_from_file_location("ppt_template_library", SCRIPT)
library = importlib.util.module_from_spec(spec)
spec.loader.exec_module(library)


class TemplateLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "original-deck"
        self.source.mkdir()
        self.storage = self.root / "templates"
        self.deck = self.root / "new-deck"
        self.deck.mkdir()
        for name, color in (("style", "green"), ("layout", "blue"), ("type", "black"), ("brand", "red"), ("preview", "white")):
            Image.new("RGB", (32, 18), color).save(self.source / f"{name}.png")
        self.config = {
            "schemaVersion": 2, "name": "测试设计", "styleRefImage": "style.png",
            "layoutSourceImage": "layout.png", "layoutRefImages": {"content": "layout.png"},
            "typography": {"roles": {"title": {"sampleText": "这是旧客户文案", "stroke": "方整中粗"}}, "references": {"title": "type.png"}},
            "referenceAssignments": [{"id": "S1", "role": "style", "path": "style.png"}, {"id": "T-title", "role": "typography", "path": "type.png"}],
        }
        self.candidate = {
            "schema_version": 1, "name": "清晰 方整 / invalid", "description": "清晰的绿色现代报告", "tags": ["中文", "绿色"],
            "design_notes": "标题采用中粗方整字形；正文保持常规字重。", "style_config": "style.json",
            "assets": [{"id": "S1", "role": "style", "path": "style.png", "source_url": "https://example.com/style", "note": "借鉴留白"},
                       {"id": "L1", "role": "layout", "path": "layout.png"}, {"id": "T-title", "role": "typography", "path": "type.png"}],
            "approval": {"status": "approved", "by": "user", "note": "保留这套风格，叫清晰方整", "approved_at": "2026-09-09T12:30:00+08:00"},
        }
        self.candidate["name"] = "清晰方整"
        self.candidate_path = self.source / "candidate.json"
        self.write_source()

    def write_source(self):
        (self.source / "style.json").write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")
        self.candidate_path.write_text(json.dumps(self.candidate, ensure_ascii=False), encoding="utf-8")

    def save(self):
        self.write_source()
        return library.save(self.candidate_path, self.storage)

    def add_brand(self):
        self.candidate["assets"].append({"id": "C1", "role": "brand", "path": "brand.png"})
        self.config["brand"] = {"name": "旧品牌", "logoPath": "brand.png"}
        self.config["referenceAssignments"].append({"id": "C1", "role": "brand", "path": "brand.png"})

    def test_requires_actual_user_approval_record(self):
        for approval in (None, {}, {"status": "approved", "by": "agent"}, {"status": "approved", "by": "user", "note": "", "approved_at": "2026-09-09T12:30:00Z"}):
            with self.subTest(approval=approval):
                self.candidate["approval"] = approval
                self.write_source()
                with self.assertRaises(ValueError):
                    library.save(self.candidate_path, self.storage)
        self.assertFalse(self.storage.exists())

    def test_approval_timestamp_requires_timezone(self):
        self.candidate["approval"]["approved_at"] = "2026-09-09T12:30:00"
        with self.assertRaisesRegex(ValueError, "ISO timestamp"):
            self.save()

    def test_dry_run_validates_without_creating_library(self):
        del self.candidate["approval"]
        self.write_source()
        result = library.save(self.candidate_path, self.storage, dry_run=True)
        self.assertEqual(result["status"], "preview")
        self.assertFalse(result["saved"])
        self.assertTrue(result["approval_required"])
        self.assertFalse(self.storage.exists())

    def test_save_rewrites_s_l_t_and_preserves_provenance(self):
        result = self.save()
        bundle = Path(result["path"])
        saved = library.read_json(bundle / "style.json")
        for slot in library.reference_slots(saved):
            path = library.get_slot(saved, slot)
            self.assertTrue(path.startswith("assets/"))
            self.assertTrue((bundle / path).is_file())
            self.assertFalse(Path(path).is_absolute())
        metadata = library.show(self.storage, result["id"])
        self.assertEqual(metadata["assets"][0]["source_url"], "https://example.com/style")
        self.assertEqual(metadata["assets"][0]["note"], "借鉴留白")
        self.assertEqual(metadata["reference_path_base"], "template_version_directory")
        self.assertEqual(metadata["integrity"], "verified")

    def test_sample_text_removed_without_altering_original(self):
        result = self.save()
        saved = library.read_json(Path(result["path"]) / "style.json")
        self.assertNotIn("sampleText", saved["typography"]["roles"]["title"])
        self.assertEqual(saved["typography"]["roles"]["title"]["stroke"], "方整中粗")
        self.assertIn("sampleText", library.read_json(self.source / "style.json")["typography"]["roles"]["title"])
        self.assertIn("typography.roles.title.sampleText", result["removed_fields"])

    def test_bundle_survives_original_deck_removal_and_move(self):
        saved = self.save()
        shutil.rmtree(self.source)
        relocated = self.root / "moved-library"
        self.storage.rename(relocated)
        result = library.apply(relocated, saved["id"], Path("theme/reused"), deck_root=self.deck)
        self.assertEqual(result["reference_path_base"], "deck_root")
        style = library.read_json(self.deck / "theme/reused/style.json")
        for slot in library.reference_slots(style):
            path = library.get_slot(style, slot)
            self.assertTrue(path.startswith("theme/reused/assets/"))
            self.assertTrue((self.deck / path).is_file())

    def test_relative_paths_use_candidate_parent_even_with_nested_config(self):
        (self.source / "config").mkdir()
        (self.source / "config/style.json").write_text(json.dumps(self.config), encoding="utf-8")
        self.candidate["style_config"] = "config/style.json"
        saved = self.save()
        self.assertTrue(saved["saved"])

    def test_unlisted_reference_fails_without_saving(self):
        self.candidate["assets"] = self.candidate["assets"][:-1]
        with self.assertRaisesRegex(ValueError, "Unlisted image reference"):
            self.save()
        self.assertFalse(self.storage.exists())

    def test_reference_assignment_must_be_listed_even_if_planned(self):
        self.config["referenceAssignments"].append({"id": "L-new", "role": "layout", "path": "brand.png", "status": "planned"})
        with self.assertRaisesRegex(ValueError, "Unlisted image reference"):
            self.save()

    def test_missing_planned_assignment_path_is_not_executable_reference(self):
        self.config["referenceAssignments"].append({"id": "L-new", "role": "layout", "status": "planned"})
        self.assertTrue(self.save()["saved"])

    def test_brand_is_not_copied_unless_explicitly_listed(self):
        saved = self.save()
        bundle = Path(saved["path"])
        self.assertEqual(len(list((bundle / "assets").iterdir())), 3)
        self.assertFalse(any(a["role"] == "brand" for a in saved["assets"]))
        self.config["brand"] = {"logoPath": "brand.png"}
        with self.assertRaisesRegex(ValueError, "Unlisted image reference"):
            self.save()

    def test_brand_assets_available_but_not_automatically_bound_after_apply(self):
        self.add_brand()
        self.config["brandIdentity"] = {"name": "旧品牌", "tagline": "旧品牌口号"}
        self.config["brandAssets"] = {"logoPath": "brand.png"}
        self.config["colors"] = {"accent": "#B7472A"}
        saved = self.save()
        result = library.apply(self.storage, saved["id"], Path("brand-theme"), deck_root=self.deck)
        self.assertEqual(len(result["available_optional_brand_assets"]), 1)
        style = library.read_json(self.deck / "brand-theme/style.json")
        self.assertNotIn("brand", style)
        self.assertNotIn("brandIdentity", style)
        self.assertNotIn("brandAssets", style)
        self.assertNotIn("旧品牌", json.dumps(style, ensure_ascii=False))
        self.assertEqual(style["colors"], {"accent": "#B7472A"})
        self.assertEqual(result["available_optional_brand_config"]["brand"]["name"], "旧品牌")
        self.assertEqual(result["available_optional_brand_config"]["brandIdentity"]["tagline"], "旧品牌口号")
        self.assertTrue(result["available_optional_brand_config"]["brand"]["logoPath"].startswith("brand-theme/assets/"))
        applied_metadata = library.read_json(self.deck / "brand-theme/template.json")
        self.assertEqual(applied_metadata["available_optional_brand_config"], result["available_optional_brand_config"])
        self.assertFalse(any(a.get("role") == "brand" for a in style["referenceAssignments"]))
        self.assertTrue((self.deck / result["available_optional_brand_assets"][0]["path"]).is_file())
        self.assertTrue(result["warnings"])

    def test_duplicate_asset_content_is_stored_once(self):
        self.candidate["assets"].append({"id": "S-other", "role": "style", "path": "style.png"})
        result = self.save()
        self.assertEqual(len(result["assets"]), 4)
        self.assertEqual(len(list((Path(result["path"]) / "assets").iterdir())), 3)

    def test_duplicate_asset_ids_rejected(self):
        self.candidate["assets"].append(dict(self.candidate["assets"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate asset id"):
            self.save()

    def test_only_decodable_image_files_are_copied(self):
        (self.source / "private.txt").write_text("secret content", encoding="utf-8")
        self.candidate["assets"].append({"id": "bad", "role": "preview", "path": "private.txt"})
        with self.assertRaisesRegex(ValueError, "Cannot fully decode"):
            self.save()
        self.assertFalse(self.storage.exists())

    def test_truncated_image_rejected(self):
        image_path = self.source / "bad.jpg"
        Image.new("RGB", (500, 500), "red").save(image_path)
        image_path.write_bytes(image_path.read_bytes()[:1000])
        self.candidate["assets"].append({"id": "bad", "role": "preview", "path": "bad.jpg"})
        with self.assertRaisesRegex(ValueError, "Cannot fully decode"):
            self.save()

    def test_credentials_in_config_rejected(self):
        self.config["provider"] = {"api_key": "do-not-store"}
        with self.assertRaisesRegex(ValueError, "credential field"):
            self.save()

    def test_remote_reference_requires_explicit_local_image(self):
        self.config["styleRefImage"] = "https://example.com/image.png"
        with self.assertRaisesRegex(ValueError, "local image path"):
            self.save()

    def test_versioning_does_not_modify_earlier_version(self):
        first = self.save()
        original = (Path(first["path"]) / "design-notes.md").read_bytes()
        self.candidate["design_notes"] = "更新的设计规则。"
        second = self.save()
        self.assertEqual(first["version"], "v001")
        self.assertEqual(second["version"], "v002")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual((Path(first["path"]) / "design-notes.md").read_bytes(), original)
        self.assertEqual(library.show(self.storage, first["id"])["version"], "v002")
        self.assertEqual(library.show(self.storage, first["id"], "v001")["version"], "v001")

    def test_search_matches_name_tags_description_and_multiple_terms(self):
        self.save()
        self.candidate["name"] = "商务黑白"
        self.candidate["tags"] = ["商务"]
        self.candidate["description"] = "黑白报告"
        self.save()
        self.assertEqual(len(library.list_templates(self.storage, "绿色 中文")["templates"]), 1)
        self.assertEqual(len(library.list_templates(self.storage, "商务")["templates"]), 1)
        self.assertEqual(len(library.list_templates(self.storage, "清晰方整")["templates"]), 1)
        self.assertEqual(len(library.list_templates(self.storage, "不存在")["templates"]), 0)

    def test_malformed_version_and_tags_reported_without_breaking_list(self):
        saved = self.save()
        malformed = self.storage / saved["id"] / "vbad"
        malformed.mkdir()
        metadata = library.read_json(Path(saved["path"]) / "template.json")
        metadata["version"] = "vbad"
        library.write_json(malformed / "template.json", metadata)
        bad_tags = self.storage / saved["id"] / "v002"
        bad_tags.mkdir()
        metadata.update({"version": "v002", "tags": "not-a-list"})
        library.write_json(bad_tags / "template.json", metadata)
        result = library.list_templates(self.storage)
        self.assertEqual(len(result["templates"]), 1)
        self.assertEqual(len(result["errors"]), 2)

    def test_busy_library_fails_without_waiting_or_creating_version(self):
        with library.library_lock(self.storage):
            with self.assertRaisesRegex(ValueError, "busy"):
                self.save()
        self.assertEqual(list(self.storage.glob("*/v*")), [])

    def test_names_and_ids_cannot_traverse_paths(self):
        for name in ("../escape", "a/b", "a\\b", "..", "bad\nname"):
            with self.subTest(name=name):
                self.candidate["name"] = name
                with self.assertRaises(ValueError):
                    self.save()
        for identifier in ("..", "../escape", "/tmp/escape"):
            with self.assertRaises(ValueError):
                library.bundle_path(self.storage, identifier)
        self.assertFalse(self.storage.exists())

    def test_hash_corruption_rejected_before_apply_creates_output(self):
        saved = self.save()
        asset = Path(saved["path"]) / saved["assets"][0]["path"]
        Image.new("RGB", (32, 18), "yellow").save(asset)
        with self.assertRaisesRegex(ValueError, "Hash"):
            library.apply(self.storage, saved["id"], Path("broken"), deck_root=self.deck)
        self.assertFalse((self.deck / "broken").exists())

    def test_style_or_notes_corruption_rejected(self):
        saved = self.save()
        (Path(saved["path"]) / "style.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "Hash mismatch"):
            library.show(self.storage, saved["id"])

    def test_manifest_path_traversal_rejected(self):
        saved = self.save()
        metadata_path = Path(saved["path"]) / "template.json"
        metadata = library.read_json(metadata_path)
        metadata["assets"][0]["path"] = "../escape.png"
        library.write_json(metadata_path, metadata)
        with self.assertRaisesRegex(ValueError, "Unsafe bundle path"):
            library.show(self.storage, saved["id"])

    def test_apply_never_overwrites_existing_even_empty_directory(self):
        saved = self.save()
        output = self.deck / "existing"
        output.mkdir()
        with self.assertRaisesRegex(ValueError, "already exists"):
            library.apply(self.storage, saved["id"], output, deck_root=self.deck)
        self.assertTrue(output.is_dir())
        self.assertEqual(list(output.iterdir()), [])

    def test_apply_output_must_be_within_deck_root(self):
        saved = self.save()
        for output in (self.root / "outside", Path("../escape"), self.deck):
            with self.subTest(output=output):
                with self.assertRaisesRegex(ValueError, "inside --deck-root"):
                    library.apply(self.storage, saved["id"], output, deck_root=self.deck)

    def test_apply_default_cwd_and_explicit_old_version(self):
        first = self.save()
        self.candidate["design_notes"] = "第二版。"
        self.save()
        with patch.object(library.Path, "cwd", return_value=self.deck):
            result = library.apply(self.storage, first["id"], Path("theme"), version="v001")
        self.assertEqual(result["version"], "v001")
        self.assertEqual((self.deck / "theme/design-notes.md").read_text(encoding="utf-8").strip(), "标题采用中粗方整字形；正文保持常规字重。")

    def test_symlinked_bundle_assets_rejected(self):
        saved = self.save()
        asset = Path(saved["path"]) / saved["assets"][0]["path"]
        asset.unlink()
        asset.symlink_to(self.source / "style.png")
        with self.assertRaisesRegex(ValueError, "escapes"):
            library.show(self.storage, saved["id"])

    def test_failed_save_leaves_no_partial_version(self):
        with patch.object(library, "write_json", side_effect=OSError("disk unavailable")):
            with self.assertRaisesRegex(OSError, "disk unavailable"):
                self.save()
        self.assertEqual(list(self.storage.glob("*/v*")), [])
        self.assertEqual(list(self.storage.glob("*/.pending-*")), [])

    def test_cli_library_flag_supported_before_and_after_subcommand(self):
        parser = library.build_parser()
        for argv in (("--library", str(self.storage), "list"), ("list", "--library", str(self.storage))):
            self.assertEqual(parser.parse_args(argv).library, self.storage)

    def test_cli_dry_run_prints_json_and_list_does_not_create_library(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = library.main(["save", "--candidate", str(self.candidate_path), "--library", str(self.storage), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())["saved"])
        self.assertFalse(self.storage.exists())
        self.assertEqual(library.list_templates(self.storage), {"templates": [], "errors": []})

    def test_default_library_honors_xdg_data_home(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "xdg")}):
            self.assertEqual(library.default_library(), self.root / "xdg/moyu-ppt/templates")

    def test_empty_xdg_data_home_uses_home_default(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": ""}), patch.object(library.Path, "home", return_value=self.root):
            self.assertEqual(library.default_library(), self.root / ".local/share/moyu-ppt/templates")

    def test_brand_asset_lists_and_image_path_extensions_cannot_leave_stale_paths(self):
        for config in ({"brand": {"assets": ["brand.png"]}}, {"custom": {"referenceImagePath": "brand.png"}}):
            self.config.update(config)
            with self.assertRaisesRegex(ValueError, "Unlisted image reference"):
                self.save()
            for key in config:
                self.config.pop(key)


if __name__ == "__main__":
    unittest.main()
