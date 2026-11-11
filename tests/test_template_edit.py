"""Static template edits survive extraction without bypassing deck validation.

All fixtures are fictional local images and confirmed test state; no generation,
credentials or external requests are involved.
"""
import contextlib
import io
import json
from pathlib import Path
import unittest

from PIL import Image

from test_pipeline import InDeck, extract, generate, page


class TemplateEditTests(InDeck):
    constants = "## 全篇常量块\n画布为PPT，统一字体，文字必须准确。\n"

    @staticmethod
    def jobs(name):
        return json.loads(Path(name).read_text())

    @staticmethod
    def snapshot(directory="exported"):
        root = Path(directory)
        return {str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    def seed_outputs(self):
        for name in ("deck-constants.txt", "manifest-anchors.json", "manifest-pages.json",
                     "manifest-all.json", "briefs/p01.txt", "briefs/p99.txt"):
            path = Path("exported") / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("KEEP EXISTING: " + name)

    def direct_extraction(self, content, *args):
        """Use the fixture state unchanged, so tests can exercise page-count gates."""
        Path("plans.md").write_text(content)
        Path("deck-state.json").write_text(json.dumps({"workflow": self.workflow}))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return extract.main(["plans.md", *args])

    def test_legacy_pages_keep_generation_fields_and_family_dependency(self):
        self.image("style.png")
        plan = self.constants + page(1) + page(2, anchor=1)
        self.assertEqual(self.extraction(plan, "--style-ref", "style.png", "--single-phase"), 0)
        anchor = self.jobs("manifest-anchors.json")[0]
        dependent = self.jobs("manifest-pages.json")[0]
        self.assertEqual([ref["id"] for ref in anchor["references"]], ["S1"])
        self.assertEqual(dependent["references"], [
            {"path": "images/p01.png", "role": "family", "id": "family"}])
        for job in [anchor, dependent, *self.jobs("manifest-all.json")]:
            self.assertNotIn("edit", job)
            self.assertNotIn("target_image", job)

    def test_edit_target_with_spaces_survives_both_manifest_modes(self):
        target = self.image("assets/templates/content panel.png")
        self.image("style.png")
        field = f'**[编辑底图]**：[target] "{target}" — 编辑内容底图'
        plan = self.constants + page(1, fields=field) + page(2, anchor=1, fields=field)
        for single in (False, True):
            with self.subTest(single_phase=single):
                flags = ["--out-dir", "exported", "--style-ref", "style.png", "--style-ref-scope", "all"]
                if single:
                    flags.append("--single-phase")
                self.assertEqual(self.extraction(plan, *flags), 0)
                jobs = self.jobs("exported/manifest-anchors.json") + self.jobs("exported/manifest-pages.json")
                if single:
                    jobs += self.jobs("exported/manifest-all.json")
                for job in jobs:
                    self.assertIs(job["edit"], True)
                    self.assertEqual(job["target_image"], target)
                    self.assertNotEqual(Path(job["output"]).resolve(), Path(target).resolve())
                    self.assertEqual(job["references"], [{"path": target, "role": "target", "id": "target"}])
                    prepared = generate.prepare_job(job)
                    self.assertEqual(prepared["references"][0]["role"], "target")
                self.assertFalse(Path("exported/images/p01.png").exists())

    def test_edit_target_keeps_explicit_style_layout_type_brand_and_size(self):
        paths = {name: self.image(name + ".png") for name in ("target", "style", "board", "layout", "type", "brand")}
        Image.new("RGB", (1536, 864), "white").save("size.png")
        Path("style.json").write_text(json.dumps({"schemaVersion": 2,
            "styleRefImage": paths["style"], "styleBoardImage": paths["board"]}))
        constants = self.constants + "- 字号锁 标题：字高 6.2%，顶 5%，左 4.6%，最宽 58%，样板 size.png\n"
        fields = ("**[编辑底图]**：[target] target.png\n"
                  "**[版式锚]**：[L-content] layout.png\n"
                  "**[字体参考]**：[T-title] type.png\n"
                  "**[参考图]**：[C1] brand.png")
        content = page(1, fields=fields, text="四字标题").replace(
            "构图与内容。", "编辑[target]，按[S1]和[S-board]的显式要求处理，字号依照[SZ1]。"
        ).replace("位置=中央", "位置=左上；角色=title；锁=标题")
        self.assertEqual(self.extraction(constants + content, "--style-config", "style.json", "--single-phase"), 0)
        for name in ("manifest-anchors.json", "manifest-all.json"):
            job = self.jobs(name)[0]
            refs = {ref["id"]: ref for ref in job["references"]}
            self.assertEqual(set(refs), {"target", "S1", "S-board", "L-content", "T-title", "C1", "SZ1"})
            self.assertEqual(refs["SZ1"]["role"], "size")
            self.assertEqual(refs["target"]["path"], "target.png")
            self.assertFalse(any(ref["role"] == "family" for ref in refs.values()))
            generate.prepare_job(job)

    def test_missing_invalid_and_malformed_targets_fail_without_writes(self):
        self.seed_outputs()
        self.image("target.png")
        Path("invalid.png").write_text("not an image")
        cases = {
            "missing": "**[编辑底图]**：[target] absent.png",
            "invalid image": "**[编辑底图]**：[target] invalid.png",
            "wrong role": "**[编辑底图]**：[family] target.png",
            "empty": "**[编辑底图]**：",
            "missing path": "**[编辑底图]**：[target]",
            "missing colon": "**[编辑底图]** [target] target.png",
            "multiple targets": "**[编辑底图]**：[target] target.png\n[target] target.png",
            "multiple fields": "**[编辑底图]**：[target] target.png\n**[编辑底图]**：[target] target.png",
            "unclosed quote": '**[编辑底图]**：[target] "target.png',
        }
        before = self.snapshot()
        for reason, field in cases.items():
            with self.subTest(reason=reason):
                # A later invalid page must not emit even the first valid page.
                plan = self.constants + page(1) + page(2, anchor=1, fields=field)
                self.assertEqual(self.extraction(plan, "--out-dir", "exported", "--single-phase"), 1)
                self.assertEqual(self.snapshot(), before)

    def test_target_cannot_be_overwritten_by_any_page_or_extracted_file(self):
        self.seed_outputs()
        for name in ("exported/images/p01.png", "exported/images/p02.png"):
            self.image(name)
        # A valid image with a non-image suffix still must not be overwritten by extraction.
        self.image("exported/briefs/p02.txt")
        self.image("exported/briefs/p99.txt")
        Path("alias.png").symlink_to(Path("exported/images/p02.png"))
        for target in ("exported/images/p02.png", "exported/images/p01.png",
                       "alias.png", "exported/briefs/p02.txt", "exported/briefs/p99.txt"):
            with self.subTest(target=target):
                before = self.snapshot()
                field = f"**[编辑底图]**：[target] {target}"
                self.assertEqual(self.extraction(self.constants + page(1) + page(2, anchor=1, fields=field),
                                                "--out-dir", "exported", "--single-phase"), 1)
                self.assertEqual(self.snapshot(), before)

    def test_edit_target_does_not_bypass_family_cycles_or_cross_family_checks(self):
        self.seed_outputs()
        self.image("target.png")
        field = "**[编辑底图]**：[target] target.png"
        cases = {
            "cycle": page(1, anchor=2, fields=field) + page(2, anchor=1, fields=field),
            "cross family": page(1, fields=field) + page(2, family="异族", anchor=1, fields=field),
            "missing anchor": page(1, fields=field) + page(2, anchor=99, fields=field),
        }
        before = self.snapshot()
        for reason, content in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(self.extraction(self.constants + content, "--out-dir", "exported"), 1)
                self.assertEqual(self.snapshot(), before)

    def test_edit_target_still_requires_confirmed_workflow_and_page_count(self):
        self.seed_outputs()
        self.image("target.png")
        plan = self.constants + page(1, fields="**[编辑底图]**：[target] target.png")
        before = self.snapshot()
        self.workflow["brief"]["page_count"] = 2
        self.assertEqual(self.direct_extraction(plan, "--out-dir", "exported"), 1)
        self.assertEqual(self.snapshot(), before)
        self.workflow["brief"]["page_count"] = 1
        self.workflow["decisions"].pop("visual")
        self.assertEqual(self.direct_extraction(plan, "--out-dir", "exported", "--single-phase"), 1)
        self.assertEqual(self.snapshot(), before)

    def test_unassigned_size_or_style_board_ids_are_rejected_atomically(self):
        self.seed_outputs()
        self.image("target.png")
        before = self.snapshot()
        for editing in (False, True):
            for ref_id in ("SZ1", "SZ99", "S-board"):
                with self.subTest(editing=editing, ref_id=ref_id):
                    fields = "**[编辑底图]**：[target] target.png" if editing else ""
                    plan = (self.constants + page(1, fields=fields)).replace(
                        "构图与内容。", f"参照[{ref_id}]。")
                    self.assertEqual(self.extraction(plan, "--out-dir", "exported", "--single-phase"), 1)
                    self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
