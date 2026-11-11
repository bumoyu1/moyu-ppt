"""Regression checks for competing title geometry; offline, no image provider."""
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import deck_runtime
import generate_slide_image


class SizeReferenceBindingTests(unittest.TestCase):
    def test_legacy_layout_binding_stays_unchanged_without_size_guide(self):
        text = deck_runtime.reference_instructions([
            {"path": "layout.png", "role": "layout", "id": "L-content"}])
        self.assertIn("仅沿用标题位置", text)

    def test_size_guide_removes_layout_authority_over_title_geometry(self):
        text = deck_runtime.reference_instructions([
            {"path": "layout.png", "role": "layout", "id": "L-content"},
            {"path": "type.png", "role": "typography", "id": "T-title", "typography_role": "title"},
            {"path": "size.png", "role": "size", "id": "SZ1"}])
        layout_line = next(line for line in text.splitlines() if "[L-content]" in line)
        self.assertNotIn("仅沿用标题位置", layout_line)
        self.assertIn("不从这张版式图继承标题位置或字号", layout_line)
        self.assertIn("不从紧裁字形图继承大小", text)
        self.assertIn("行宽自然形成", text)
        self.assertIn("不迁移其字体", text)

    def test_missing_named_size_guide_fails_in_credential_free_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "not uploaded"):
                generate_slide_image.prepare_job({
                    "prompt": "Title geometry follows [SZ1].", "output": str(Path(directory) / "out.png")})

    def test_size_guide_binding_is_exported_with_image_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            guide = directory / "guide.png"
            Image.new("RGB", (160, 90), "white").save(guide)
            package = generate_slide_image.prepare_job({
                "prompt": "Title geometry follows [SZ1].", "output": str(directory / "out.png"),
                "references": [{"path": str(guide), "role": "size", "id": "SZ1"}]})
            self.assertEqual(package["references"][0]["role"], "size")
            self.assertIn("sha256", package["references"][0])
            self.assertIn("完整画幅", package["resolved_prompt"])

    def test_tightly_cropped_glyph_strip_cannot_be_declared_as_size_guide(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            guide = directory / "tight-strip.png"
            Image.new("RGB", (500, 70), "white").save(guide)
            with self.assertRaisesRegex(ValueError, "ratio"):
                generate_slide_image.prepare_job({
                    "prompt": "Title follows [SZ1].", "output": str(directory / "out.png"),
                    "references": [{"path": str(guide), "role": "size", "id": "SZ1"}]})
            self.assertFalse((directory / "out.png").exists())


if __name__ == "__main__":
    unittest.main()
