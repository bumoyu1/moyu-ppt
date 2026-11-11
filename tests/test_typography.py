"""Deterministic contract tests; these do not claim image quality evidence."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "typography_specimen.py"
SPEC = importlib.util.spec_from_file_location("typography_specimen", SCRIPT)
typography = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(typography)


class TypographySpecimenTests(unittest.TestCase):
    def test_exact_characters_are_preserved_in_prompt_and_metadata(self):
        text = '  简体「复盘」與繁體「複盤」\r\n下降 −12.5%，USD 1,200 / Q3\t\n'
        result = typography.build_specimen({}, "title", text)
        self.assertEqual(result["metadata"]["expected_text"], text)
        self.assertEqual(result["prompt"].count(text), 1)
        self.assertIn("不得转换、改写或补字", result["prompt"])
        self.assertFalse(result["metadata"]["image_generated"])

    def test_only_selected_role_glyph_constraints_enter_the_prompt(self):
        config = {
            "colors": {"primaryBackground": "FORBIDDEN_PURPLE"},
            "layoutRefImages": {"cover": "FORBIDDEN_LAYOUT"},
            "typography": {"roles": {
                "title": {"familyIntent": "CHOSEN_TITLE", "effects": ["FORBIDDEN_NEON"]},
                "body": {"familyIntent": "FORBIDDEN_BODY", "sampleText": "FORBIDDEN_SAMPLE"},
            }},
        }
        result = typography.build_specimen(config, "title", "当前标题")
        self.assertIn("CHOSEN_TITLE", result["prompt"])
        for unwanted in ("FORBIDDEN_PURPLE", "FORBIDDEN_LAYOUT", "FORBIDDEN_NEON", "FORBIDDEN_BODY", "FORBIDDEN_SAMPLE"):
            self.assertNotIn(unwanted, result["prompt"])
        self.assertEqual(result["metadata"]["omitted_effects"], ["FORBIDDEN_NEON"])
        self.assertEqual(result["metadata"]["reference_id"], "T-title")

    def test_sample_fallback_is_role_specific_and_explicit_text_wins(self):
        config = {"typography": {"roles": {
            "title": {"sampleText": "标题样本"},
            "body": {"sampleText": "正文样本"},
        }}}
        self.assertEqual(typography.build_specimen(config, "title")["metadata"]["expected_text"], "标题样本")
        self.assertEqual(typography.build_specimen(config, "title", "新标题")["metadata"]["expected_text"], "新标题")
        with self.assertRaises(ValueError):
            typography.build_specimen(config, "data")

    def test_legacy_config_has_conservative_role_defaults(self):
        result = typography.build_specimen({"id": "old-style", "styleWeight": 0}, "body", "事实与条件均保留。")
        self.assertIn("常规无衬线", result["prompt"])
        self.assertNotIn("展示字", result["prompt"])
        self.assertEqual(result["metadata"]["scope"], "glyph-only")

    def test_locked_policy_and_invalid_inputs_fail_before_writing(self):
        for key, value in (("transferColor", True), ("transferSampleText", True),
                           ("transferLayout", True), ("preserveExactText", False),
                           ("scope", "whole-image"), ("mode", "off")):
            config = {"typography": {"referencePolicy": {key: value}}}
            with self.subTest(key=key), self.assertRaises(ValueError):
                typography.build_specimen(config, "title", "需要保真")
        for role, text in (("heading", "标题"), ("title", " \n"), ("title", "坏\x00字")):
            with self.subTest(role=role, text=text), self.assertRaises(ValueError):
                typography.build_specimen({}, role, text)
        with self.assertRaises(ValueError):
            typography.build_specimen({"typography": {"roles": {"title": {"spacing": {"lineHeight": 0}}}}}, "title", "标题")

    def test_prepared_jobs_are_portable_and_no_image_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            result = typography.prepare_specimen({}, "data", "−12.5% / 2026", directory)
            jobs = json.loads(Path(result["jobs_file"]).read_text())
            self.assertEqual(len(jobs), 1)
            self.assertTrue(Path(jobs[0]["prompt_file"]).is_absolute())
            self.assertTrue(Path(jobs[0]["output"]).is_absolute())
            self.assertNotIn("references", jobs[0])
            self.assertNotIn("prefix_file", jobs[0])
            self.assertFalse(Path(result["planned_image"]).exists())
            self.assertEqual(json.loads(Path(result["specimen_file"]).read_text())["expected_text"], "−12.5% / 2026")

    def test_reprepare_identical_is_safe_and_changed_text_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            result = typography.prepare_specimen({}, "title", "第一行\r\n第二行", directory)
            original = Path(result["prompt_file"]).read_bytes()
            typography.prepare_specimen({}, "title", "第一行\r\n第二行", directory)
            with self.assertRaises(ValueError):
                typography.prepare_specimen({}, "title", "不同内容", directory)
            self.assertEqual(Path(result["prompt_file"]).read_bytes(), original)
            typography.prepare_specimen({}, "title", "不同内容", directory, name="title-v2")
            self.assertEqual(Path(result["prompt_file"]).read_bytes(), original)

    def test_untracked_image_is_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "title.png"
            image.write_bytes(b"existing image")
            with self.assertRaises(ValueError):
                typography.prepare_specimen({}, "title", "标题", directory)
            self.assertEqual(image.read_bytes(), b"existing image")
            self.assertFalse((Path(directory) / "title.prompt.txt").exists())

    def test_cli_preserves_utf8_file_with_crlf_and_no_model_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "style.json").write_text('{"id":"legacy"}')
            raw = '  發展 / 发展\r\n−0.05%\n'.encode("utf-8")
            (root / "text.txt").write_bytes(raw)
            result = subprocess.run([
                sys.executable, str(SCRIPT), "--config", str(root / "style.json"),
                "--role", "title", "--text-file", str(root / "text.txt"),
                "--output-dir", str(root / "out"),
            ], capture_output=True, text=True, check=True)
            output = json.loads(result.stdout)
            metadata = json.loads(Path(output["specimen_file"]).read_text())
            self.assertEqual(metadata["expected_text"].encode("utf-8"), raw)
            self.assertIn(raw, Path(output["prompt_file"]).read_bytes())
            self.assertFalse(Path(output["planned_image"]).exists())

    def test_sample_config_produces_five_isolated_roles(self):
        config_path = SCRIPT.parents[1] / "assets" / "examples" / "typography-style.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for role in typography.ROLES:
            with self.subTest(role=role):
                result = typography.build_specimen(config, role)
                self.assertEqual(result["metadata"]["role"], role)
                self.assertEqual(result["metadata"]["expected_text"], config["typography"]["roles"][role]["sampleText"])


if __name__ == "__main__":
    unittest.main()
