"""Local contract regressions: no credentials, external providers or paid calls."""
import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from PIL import Image

# Keep tests off any real per-user config or stored keys.
os.environ.setdefault("MOYU_PPT_CONFIG_DIR", tempfile.mkdtemp(prefix="ppt-config-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import deck_runtime as runtime
import extract_briefs as extract
import generate_slide_image as generate


def png(color="white", size=(32, 16)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def page(number, family="日光", anchor=None, fields="", text="标题"):
    note = "本页为族锚定页" if anchor is None else f"锚定页 P{anchor}"
    return (f"## 第{number}页设计方案\n**[场景族]**：{family}（{note}）\n{fields}\n"
            f"**[空间定位图]**\n构图与内容。\n**可见文字清单**：\n可见文字={text}；位置=中央\n")


class InDeck(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppt-pipeline-test-")
        self.previous = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, self.previous)
        self.addCleanup(self.temp.cleanup)
        # Any unexpected network call fails the test instead of reaching a service.
        self.network = mock.patch.object(generate.urllib.request, "urlopen", side_effect=AssertionError("Network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        # Explicitly fictional user evidence for contract tests only. These
        # records never authorize a real deck or migrate legacy user state.
        Path("fixture-visual.md").write_text("TEST ONLY: visual and five typography roles shown.")
        Path("fixture-refs.md").write_text("TEST ONLY: reviewed reference shortlist.")
        roles = {name: {"familyIntent": "TEST ONLY: readable sans serif"}
                 for name in ("display", "title", "body", "label", "data")}
        Path("fixture-style.json").write_text(json.dumps({"schemaVersion": 2, "typography": {"roles": roles}}))
        decision = {"status": "confirmed", "source": "user", "evidence": "TEST FIXTURE: confirm presented proposal",
                    "confirmed_at": "2026-09-09T10:00:00+08:00"}
        self.workflow = {"schema_version": 1, "mode": "standard",
            "brief": {"page_count": 1, "aspect_ratio": "16:9", "audience": "test audience", "purpose": "test presentation"},
            "decisions": {"intake": dict(decision), "visual": dict(decision)},
            "artifacts": {"visual_proposal": "fixture-visual.md", "inspiration_review": "fixture-refs.md", "style_config": "fixture-style.json"},
            "typography_decision": {"specimen": "skip", "reason": "TEST ONLY: ordinary type", "summary": "TEST ONLY: five readable text roles"},
            "pilot_pages": [1]}
        Path("deck-state.json").write_text(json.dumps({"workflow": self.workflow}))

    def image(self, name, color="white"):
        Path(name).parent.mkdir(parents=True, exist_ok=True)
        Path(name).write_bytes(png(color))
        return name

    def extraction(self, content, *args):
        Path("plans.md").write_text(content)
        self.workflow["brief"]["page_count"] = max([number for number, _ in extract.split_pages(content)] or [1])
        if "--style-config" in args:
            style_path = args[args.index("--style-config") + 1]
            self.workflow["artifacts"]["style_config"] = style_path
            config = json.loads(Path(style_path).read_text())
            config.setdefault("typography", {}).setdefault("roles", json.loads(Path("fixture-style.json").read_text())["typography"]["roles"])
            Path(style_path).write_text(json.dumps(config))
        else:
            self.workflow["artifacts"]["style_config"] = "fixture-style.json"
        Path("deck-state.json").write_text(json.dumps({"workflow": self.workflow}))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return extract.main(["plans.md", *args])

    def package(self, **extra):
        job = {"prompt": "Render the supplied slide text.", "output": "images/p01.png", "size": "32x16", **extra}
        return generate.prepare_job(job)

    def response(self, color="white"):
        return {"data": [{"b64_json": base64.b64encode(png(color)).decode()}]}


class ExtractionTests(InDeck):
    constants = "## 全篇常量块\n画布为PPT，统一字体，文字必须准确。\n"

    def test_quoted_and_unambiguous_spaced_paths_preserve_annotations(self):
        paths = {"layout": "assets/layout samples/cover - one.png",
                 "title": "assets/type samples/title.png", "brand": "assets/brand icons/logo main.png"}
        for path in paths.values():
            self.image(path)
        fields = ('**[版式锚]**：[L-cover] "' + paths["layout"] + '" — 沿用版式\n'
                  '**[字体参考]**：[T-title] ' + paths["title"] + ' — 仅标题\n'
                  "**[参考图]**：[C1] '" + paths["brand"] + "'；右上角原样出现")
        self.assertEqual(self.extraction(self.constants + page(1, fields=fields)), 0)
        refs = json.loads(Path("manifest-anchors.json").read_text())[0]["references"]
        self.assertEqual([r["path"] for r in refs], [paths["layout"], paths["title"], paths["brand"]])
        generate.prepare_job(json.loads(Path("manifest-anchors.json").read_text())[0])

    def test_unclosed_quote_is_an_atomic_preflight_failure(self):
        Path("manifest-anchors.json").write_text("unchanged")
        fields = '**[字体参考]**：[T-title] "assets/type samples/title.png — 仅标题'
        self.assertEqual(self.extraction(self.constants + page(1, fields=fields)), 1)
        self.assertEqual(Path("manifest-anchors.json").read_text(), "unchanged")

    def test_invalid_inputs_do_not_mutate_existing_artifacts(self):
        Path("briefs").mkdir()
        expected = {"deck-constants.txt": "OLD", "manifest-anchors.json": "OLD ANCHORS",
                    "manifest-pages.json": "OLD PAGES", "manifest-all.json": "OLD ALL", "briefs/p01.txt": "OLD BRIEF"}
        for path, value in expected.items():
            Path(path).write_text(value)
        invalid = {
            "constants": page(1),
            "duplicate": self.constants + page(1) + page(1),
            "gap": self.constants + page(1) + page(3, anchor=1),
            "empty_brief": self.constants + "## 第1页设计方案\n**[场景族]**：日光（本页为族锚定页）\n**[空间定位图]**\n",
            "text_list": (self.constants + page(1)).replace("可见文字=标题", "没有清单"),
            "missing_anchor": self.constants + page(1) + page(2, anchor=99),
            "different_family": self.constants + page(1) + page(2, family="暗夜", anchor=1),
            "cycle": self.constants + page(1, anchor=2) + page(2, anchor=1),
            "no_family": (self.constants + page(1)).replace("**[场景族]**", "**[其它]**"),
            "missing_asset": self.constants + page(1, fields="**[参考图]**：[C1] absent.png"),
        }
        for reason, content in invalid.items():
            with self.subTest(reason=reason):
                self.assertEqual(self.extraction(content), 1)
                self.assertEqual({path: Path(path).read_text() for path in expected}, expected)

    def test_success_cleans_stale_briefs_and_single_phase_manifest(self):
        Path("briefs").mkdir()
        Path("briefs/p99.txt").write_text("STALE")
        Path("manifest-all.json").write_text("STALE")
        self.assertEqual(self.extraction(self.constants + page(1) + page(3, anchor=1), "--allow-partial"), 0)
        self.assertFalse(Path("briefs/p99.txt").exists())
        self.assertFalse(Path("manifest-all.json").exists())
        jobs = json.loads(Path("manifest-pages.json").read_text())
        self.assertEqual(jobs[0]["page"], 3)
        self.assertEqual(jobs[0]["references"], [{"path": "images/p01.png", "role": "family", "id": "family"}])

    def test_weights_are_independent_and_font_references_are_per_page(self):
        for name in ["style.png", "layout.png", "title.png", "body.png", "logo.png"]:
            self.image(name)
        config = {"schemaVersion": 2, "styleWeight": 0, "styleRefImage": "style.png", "typography": {
            "references": {"title": "title.png", "body": "body.png"}}}
        Path("style.json").write_text(json.dumps(config))
        fields = "**[版式锚]**：[L-cover] layout.png\n**[字体参考]**：[T-title]\n[T-body] body.png\n**[参考图]**：[C1] logo.png"
        self.assertEqual(self.extraction(self.constants + page(1, fields=fields) + page(2, anchor=1), "--style-config", "style.json"), 0)
        anchor = json.loads(Path("manifest-anchors.json").read_text())[0]
        self.assertEqual([r["role"] for r in anchor["references"]], ["layout", "typography", "typography", "brand"])
        other = json.loads(Path("manifest-pages.json").read_text())[0]
        self.assertEqual([r["role"] for r in other["references"]], [])
        config["layoutWeight"] = 0
        config["typography"]["referencePolicy"] = {"mode": "off"}
        Path("style.json").write_text(json.dumps(config))
        self.assertEqual(self.extraction(self.constants + page(1, fields=fields), "--style-config", "style.json"), 0)
        self.assertEqual([r["role"] for r in json.loads(Path("manifest-anchors.json").read_text())[0]["references"]], ["brand"])

    def test_multiple_candidates_for_one_font_role_fail_before_output(self):
        self.image("font.png")
        Path("style.json").write_text(json.dumps({"typography": {"references": {"title": ["font.png", "font.png"]}}}))
        self.assertEqual(self.extraction(self.constants + page(1), "--style-config", "style.json"), 1)
        fields = "**[字体参考]**：[T-title] font.png；[T-title] font.png"
        self.assertEqual(self.extraction(self.constants + page(1, fields=fields)), 1)
        self.assertFalse(Path("manifest-anchors.json").exists())

    def test_legacy_low_weight_compatibility_and_v2_independent_default(self):
        self.image("layout.png")
        plan = self.constants + page(1, fields="**[版式锚]**：[L-cover] layout.png")
        for config, expected_roles in [({"styleWeight": 30}, []),
                ({"schemaVersion": 2, "styleWeight": 30}, ["layout"]),
                ({"styleWeight": 30, "layoutWeight": 90}, ["layout"])]:
            with self.subTest(config=config):
                Path("style.json").write_text(json.dumps(config))
                self.assertEqual(self.extraction(plan, "--style-config", "style.json"), 0)
                self.assertEqual([r["role"] for r in json.loads(Path("manifest-anchors.json").read_text())[0]["references"]], expected_roles)

    def test_out_dir_job_paths_resolve_from_deck_cwd(self):
        self.image("style.png")
        self.assertEqual(self.extraction(self.constants + page(1) + page(2, anchor=1),
                                        "--out-dir", "generated", "--style-ref", "style.png"), 0)
        anchor = json.loads(Path("generated/manifest-anchors.json").read_text())[0]
        self.assertEqual(anchor["prompt_file"], "generated/briefs/p01.txt")
        self.assertEqual(anchor["output"], "generated/images/p01.png")
        self.assertEqual(anchor["references"][0]["path"], "style.png")
        self.assertTrue(Path(anchor["prompt_file"]).is_file())
        self.assertEqual(generate.prepare_job(anchor)["references"][0]["id"], "S1")

    def test_explicit_single_phase_needs_no_style_or_family_images(self):
        self.image("font.png")
        self.image("layout.png")
        Path("style.json").write_text(json.dumps({"schemaVersion": 2, "styleWeight": 0,
            "typography": {"references": {"title": "font.png"}}}))
        fields = "**[字体参考]**：[T-title]\n**[版式锚]**：[L-content] layout.png"
        plan = self.constants + page(1) + page(2, anchor=1, fields=fields)
        self.assertEqual(self.extraction(plan, "--style-config", "style.json", "--single-phase"), 0)
        jobs = json.loads(Path("manifest-all.json").read_text())
        self.assertEqual(jobs[0]["references"], [])
        self.assertEqual([r["role"] for r in jobs[1]["references"]], ["layout", "typography"])
        self.assertFalse(Path("images/p01.png").exists())
        for job in jobs:
            generate.prepare_job(job)

    def test_low_style_weight_disables_family_images_without_single_phase(self):
        self.image("font.png")
        self.image("layout.png")
        fields = "**[字体参考]**：[T-title] font.png\n**[版式锚]**：[L-content] layout.png"
        plan = self.constants + page(1) + page(2, anchor=1, fields=fields)
        for weight in [0, 30, 50, 69, 70]:
            with self.subTest(styleWeight=weight):
                Path("style.json").write_text(json.dumps({"schemaVersion": 2, "styleWeight": weight}))
                self.assertEqual(self.extraction(plan, "--style-config", "style.json"), 0)
                job = json.loads(Path("manifest-pages.json").read_text())[0]
                self.assertEqual([r["role"] for r in job["references"]],
                                 (["family"] if weight >= 70 else []) + ["layout", "typography"])
                if weight < 70:
                    generate.prepare_job(job)
                    self.assertFalse(Path("images/p01.png").exists())

    def test_rhythm_pages_follow_style_board_instead_of_anchor_slide(self):
        self.image("board.png")
        plan = (self.constants + page(1) + page(2, anchor=1, fields="**[节奏]**：稳定")
                + page(3, anchor=1, fields="**[节奏]**：变奏（只变：视角）")
                + page(4, anchor=1, fields="**[节奏]**：亮点（章节开场）"))
        Path("style.json").write_text(json.dumps({"schemaVersion": 2, "styleBoardImage": "board.png"}))
        self.assertEqual(self.extraction(plan, "--style-config", "style.json"), 0)
        jobs = {job["page"]: job for job in json.loads(Path("manifest-pages.json").read_text())}
        self.assertEqual([r["id"] for r in jobs[2]["references"]], ["family"])
        self.assertEqual([r["id"] for r in jobs[3]["references"]], ["S-board"])
        self.assertEqual([r["id"] for r in jobs[4]["references"]], ["S-board"])
        self.assertEqual(json.loads(Path("manifest-anchors.json").read_text())[0]["references"], [])
        generate.prepare_job(jobs[3])  # no dependency on the not-yet-generated anchor image
        # Without a board, rhythm pages keep the family anchor; low weight uploads neither.
        Path("style.json").write_text(json.dumps({"schemaVersion": 2}))
        self.assertEqual(self.extraction(plan, "--style-config", "style.json"), 0)
        jobs = {job["page"]: job for job in json.loads(Path("manifest-pages.json").read_text())}
        self.assertEqual([r["id"] for r in jobs[3]["references"]], ["family"])
        Path("style.json").write_text(json.dumps({"schemaVersion": 2, "styleWeight": 50, "styleBoardImage": "board.png"}))
        self.assertEqual(self.extraction(plan, "--style-config", "style.json"), 0)
        self.assertEqual(json.loads(Path("manifest-pages.json").read_text())[1]["references"], [])

    def test_one_image_may_be_both_the_style_reference_and_the_board(self):
        self.image("board.png")
        plan = (self.constants + page(1) + page(2, anchor=1, fields="**[节奏]**：稳定")
                + page(3, anchor=1, fields="**[节奏]**：变奏（变：视角）"))
        Path("style.json").write_text(json.dumps({"schemaVersion": 2, "styleRefImage": "board.png", "styleBoardImage": "board.png"}))
        self.assertEqual(self.extraction(plan, "--style-config", "style.json"), 0)  # no "same path, two IDs" failure
        jobs = {job["page"]: job for job in json.loads(Path("manifest-pages.json").read_text())}
        self.assertEqual([r["id"] for r in jobs[2]["references"]], ["family"])
        self.assertEqual([(r["id"], r["path"]) for r in jobs[3]["references"]], [("S1", "board.png")])  # uploaded once
        self.assertEqual([r["id"] for r in json.loads(Path("manifest-anchors.json").read_text())[0]["references"]], ["S1"])

    def test_rhythm_pages_drop_anchor_when_a_style_image_reaches_every_page(self):
        self.image("cast.png")
        plan = (self.constants + page(1) + page(2, anchor=1) + page(3, anchor=1, fields="**[节奏]**：亮点（总览）"))
        Path("style.json").write_text(json.dumps({"schemaVersion": 2, "styleRefImage": "cast.png"}))
        self.assertEqual(self.extraction(plan, "--style-config", "style.json", "--style-ref-scope", "all"), 0)
        jobs = {job["page"]: job for job in json.loads(Path("manifest-pages.json").read_text())}
        self.assertEqual([r["id"] for r in jobs[2]["references"]], ["family", "S1"])
        self.assertEqual([r["id"] for r in jobs[3]["references"]], ["S1"])
        # With the default anchors-only scope the style image is not on other pages, so keep the anchor.
        self.assertEqual(self.extraction(plan, "--style-config", "style.json"), 0)
        jobs = {job["page"]: job for job in json.loads(Path("manifest-pages.json").read_text())}
        self.assertEqual([r["id"] for r in jobs[3]["references"]], ["family"])

    def test_unknown_rhythm_value_fails_before_writing(self):
        plan = self.constants + page(1) + page(2, anchor=1, fields="**[节奏]**：随意")
        self.assertNotEqual(self.extraction(plan), 0)
        self.assertFalse(Path("manifest-pages.json").exists())

    def test_disabled_stable_reference_instructions_fail_before_writing(self):
        self.image("font.png")
        self.image("layout.png")
        self.image("style.png")
        cases = [
            ({"typography": {"referencePolicy": {"mode": "off"}}}, "T-title", "**[字体参考]**：[T-title] font.png"),
            ({"layoutWeight": 0}, "L-content", "**[版式锚]**：[L-content] layout.png"),
            ({"styleWeight": 0, "styleRefImage": "style.png"}, "S1", ""),
        ]
        for config, ref_id, fields in cases:
            with self.subTest(ref_id=ref_id):
                Path("style.json").write_text(json.dumps({"schemaVersion": 2, **config}))
                Path("manifest-anchors.json").write_text("old manifest")
                plan = self.constants + page(1, fields=fields).replace("构图与内容。", f"只从 [{ref_id}] 借指定视觉特征。")
                self.assertEqual(self.extraction(plan, "--style-config", "style.json"), 1)
                self.assertEqual(Path("manifest-anchors.json").read_text(), "old manifest")


class PreparationTests(InDeck):
    def test_reference_mapping_is_ordered_and_stable_ids_are_bound(self):
        for name in ["target.png", "family.png", "style.png", "layout.png", "font.png", "logo.png"]:
            self.image(name)
        refs = [{"path": "logo.png", "role": "brand", "id": "C1"},
                {"path": "font.png", "role": "typography", "id": "T-title"},
                {"path": "style.png", "role": "style", "id": "S1"},
                {"path": "layout.png", "role": "layout", "id": "L-cover"},
                {"path": "family.png", "role": "family", "id": "family"}, "target.png"]
        package = self.package(target_image="target.png", references=refs, prompt="编辑[target]，使用[T-title]和[L-cover]。")
        self.assertEqual([r["role"] for r in package["references"]], ["target", "style", "family", "layout", "typography", "brand"])
        self.assertIn("参考图4 [L-cover]", package["resolved_prompt"])
        self.assertIn("参考图5 [T-title]", package["resolved_prompt"])
        with self.assertRaisesRegex(ValueError, "conflicting roles"):
            self.package(target_image="target.png", references=[{"path": "target.png", "role": "style", "id": "S1"}])

    def test_positional_legacy_numbers_and_undeclared_ids_are_rejected(self):
        self.image("ref.png")
        with self.assertRaisesRegex(ValueError, "stable IDs"):
            self.package(prompt="参考图1提供风格", references=["ref.png"])
        with self.assertRaisesRegex(ValueError, "not uploaded"):
            self.package(prompt="使用[T-title]", references=[])
        prepared = self.package(references=["ref.png"])
        self.assertEqual(prepared["references"][0]["role"], "reference")

    def test_edit_requires_separate_target_and_versioned_output(self):
        self.image("original.png")
        with self.assertRaisesRegex(ValueError, "target_image"):
            self.package(edit=True, references=["original.png"])
        with self.assertRaisesRegex(ValueError, "versioned output"):
            self.package(target_image="original.png", output="original.png")

    def test_hash_tracks_prompt_model_size_and_reference_bytes(self):
        self.image("ref.png")
        original = self.package(references=["ref.png"])
        self.assertNotEqual(original["input_hash"], self.package(prompt="Different", references=["ref.png"])["input_hash"])
        self.assertNotEqual(original["input_hash"], self.package(model="new-model", references=["ref.png"])["input_hash"])
        self.assertNotEqual(original["input_hash"], self.package(size="64x32", references=["ref.png"])["input_hash"])
        self.image("ref.png", "red")
        self.assertNotEqual(original["input_hash"], self.package(references=["ref.png"])["input_hash"])

    def test_dry_run_does_not_read_credentials_or_write_state(self):
        captured = io.StringIO()
        with mock.patch.object(generate, "resolve_api_key", side_effect=AssertionError("No key")), contextlib.redirect_stdout(captured):
            result = generate.main(["--prompt", "text", "--output", "images/p01.png", "--size", "32x16", "--dry-run"])
        self.assertEqual(result, 0)
        self.assertFalse(Path("run-state.json").exists())
        package = json.loads(captured.getvalue())["jobs"][0]
        self.assertEqual(package["resolved_prompt"], "text")
        self.assertEqual(package["size"], "32x16")
        self.assertIn("not provider-validated", package["capabilities"]["capability_source"])

    def test_prompt_file_preserves_internal_crlf(self):
        Path("prompt.txt").write_bytes(b"Start\r\nExact line\r\nEnd")
        self.assertEqual(generate.read_prompt({"prompt_file": "prompt.txt"}), "Start\r\nExact line\r\nEnd")

    def test_entire_batch_preflights_before_key_lookup(self):
        Path("jobs.json").write_text(json.dumps([{"prompt": "good", "output": "one.png", "size": "32x16"},
            {"prompt": "bad", "output": "two.png", "references": ["absent.png"]}]))
        with mock.patch.object(generate, "resolve_api_key", side_effect=AssertionError("No key")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(generate.main(["--batch", "jobs.json"]), 1)
        self.assertFalse(Path("run-state.json").exists())


class GenerationStateTests(InDeck):
    def test_state_lock_blocks_overlap_and_releases_on_context_exit(self):
        with generate.RunState() as first:
            first.put("a.png", {"status": "success", "input_hash": "fixture"})
            with self.assertRaises((BlockingIOError, OSError)):
                with generate.RunState():
                    self.fail("Concurrent state owner should not acquire the lock")
        with generate.RunState() as next_owner:
            self.assertEqual(next_owner.get("a.png")["input_hash"], "fixture")

    def test_success_reuse_force_and_secret_free_ledger(self):
        package = self.package()
        with generate.RunState() as state, mock.patch.object(generate, "submit_prepared", return_value=self.response()) as submit:
            generate.execute_prepared("TEST_SECRET_VALUE", package, state)
            generate.execute_prepared(lambda: self.fail("Reuse must not read keys"), package, state)
            self.assertEqual(submit.call_count, 1)
            generate.execute_prepared("TEST_SECRET_VALUE", package, state, force=True)
            self.assertEqual(submit.call_count, 2)
        serialized = Path("run-state.json").read_text()
        self.assertNotIn("TEST_SECRET_VALUE", serialized)
        self.assertNotIn("b64_json", serialized)
        self.assertEqual(json.loads(serialized)["jobs"][str(Path(package["output"]).resolve())]["status"], "success")

    def test_state_records_requested_dimensions_in_job_and_run_snapshot(self):
        package = self.package()
        with generate.RunState() as state, mock.patch.object(generate, "submit_prepared", return_value=self.response()):
            state.start_run([package])
            generate.execute_prepared("key", package, state)
        data = json.loads(Path("run-state.json").read_text())
        for record in [data["jobs"][str(Path(package["output"]).resolve())], data["runs"][-1]["jobs"][0]]:
            self.assertEqual(record["size"], "32x16")
            self.assertEqual(record["aspect_ratio"], "32:16")

    def test_unknown_submission_is_not_retried_even_with_retry_failed(self):
        package = self.package()
        with generate.RunState() as state, mock.patch.object(generate, "submit_prepared", side_effect=TimeoutError("TEST_SECRET_VALUE")) as submit:
            with self.assertRaisesRegex(ValueError, "outcome unknown"):
                generate.execute_prepared("TEST_SECRET_VALUE", package, state)
            with self.assertRaisesRegex(ValueError, "No automatic resubmission"):
                generate.execute_prepared("TEST_SECRET_VALUE", package, state, retry_failed=True)
            self.assertEqual(submit.call_count, 1)
        self.assertNotIn("TEST_SECRET_VALUE", Path("run-state.json").read_text())

    def test_same_inputs_unanswered_twice_reports_the_count(self):
        package = self.package()
        with generate.RunState() as state, mock.patch.object(
                generate, "submit_prepared", side_effect=[TimeoutError("cut"), TimeoutError("cut"), self.response()]):
            with self.assertRaises(ValueError) as first:
                generate.execute_prepared("key", package, state)
            self.assertNotIn("times", str(first.exception))
            with self.assertRaisesRegex(ValueError, "2 times.*tell the user the count.*fewer reference images"):
                generate.execute_prepared("key", package, state, force=True)
            record = state.get(package["output"])
            self.assertEqual((record["status"], record["unanswered"]), ("unknown", 2))
            generate.execute_prepared("key", package, state, force=True)
            self.assertEqual(state.get(package["output"])["status"], "success")

    def test_changed_inputs_start_the_unanswered_count_again(self):
        package = self.package()
        with generate.RunState() as state, mock.patch.object(
                generate, "submit_prepared", side_effect=[TimeoutError("cut"), TimeoutError("cut")]):
            with self.assertRaises(ValueError):
                generate.execute_prepared("key", package, state)
            changed = dict(package, input_hash="f" * 64)
            with self.assertRaises(ValueError) as second:
                generate.execute_prepared("key", changed, state, force=True)
            self.assertNotIn("times", str(second.exception))
            self.assertEqual(state.get(package["output"])["unanswered"], 1)

    def test_rejected_request_needs_explicit_retry_and_does_not_hide_error(self):
        package = self.package()
        rejection = urllib.error.HTTPError("https://provider.invalid", 429, "limited", {}, None)
        with generate.RunState() as state, mock.patch.object(generate, "submit_prepared", side_effect=[rejection, self.response()]) as submit:
            with self.assertRaisesRegex(ValueError, "HTTP 429"):
                generate.execute_prepared("key", package, state)
            with self.assertRaisesRegex(ValueError, "retry-failed"):
                generate.execute_prepared("key", package, state)
            generate.execute_prepared("key", package, state, retry_failed=True)
            self.assertEqual(submit.call_count, 2)

    def test_accepted_url_can_resume_download_without_new_submission_or_key(self):
        package = self.package()
        with generate.RunState() as state, mock.patch.object(generate, "submit_prepared", return_value={"data": [{"url": "https://images.invalid/generated.png"}]}) as submit:
            with mock.patch.object(generate.urllib.request, "urlopen", side_effect=TimeoutError()):
                with self.assertRaises(ValueError):
                    generate.execute_prepared("secret", package, state)
            self.assertEqual(state.get(package["output"])["status"], "accepted")
            with mock.patch.object(generate.urllib.request, "urlopen", return_value=io.BytesIO(png())):
                generate.execute_prepared(lambda: self.fail("Download must not read key"), package, state)
            self.assertEqual(submit.call_count, 1)
            self.assertEqual(state.get(package["output"])["status"], "success")

    def test_corrupt_download_is_refetched_without_resubmitting_generation(self):
        package = self.package()
        with generate.RunState() as state, mock.patch.object(generate, "submit_prepared", return_value={"data": [{"url": "https://images.invalid/generated.png"}]}) as submit:
            with mock.patch.object(generate.urllib.request, "urlopen", return_value=io.BytesIO(b"temporary HTML error")):
                with self.assertRaises(ValueError):
                    generate.execute_prepared("secret", package, state)
            self.assertNotIn("pending_image", state.get(package["output"]))
            with mock.patch.object(generate.urllib.request, "urlopen", return_value=io.BytesIO(png())):
                generate.execute_prepared("secret", package, state)
            self.assertEqual(submit.call_count, 1)

    def test_bad_image_and_wrong_dimensions_never_replace_existing_output(self):
        output = self.image("images/p01.png", "blue")
        original = Path(output).read_bytes()
        for data in [b"not an image", png(size=(16, 16))]:
            with self.subTest(data_length=len(data)):
                payload = {"data": [{"b64_json": base64.b64encode(data).decode()}]}
                with self.assertRaises(ValueError):
                    generate.save_from_response(payload, output, "32x16", "2:1")
                self.assertEqual(Path(output).read_bytes(), original)

    def test_corrupt_accepted_response_cannot_trigger_new_generation(self):
        package = self.package()
        response = {"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]}
        with generate.RunState() as state, mock.patch.object(generate, "submit_prepared", return_value=response) as submit:
            with self.assertRaises(ValueError):
                generate.execute_prepared("key", package, state)
            with self.assertRaises(ValueError):
                generate.execute_prepared("key", package, state, retry_failed=True)
            self.assertEqual(submit.call_count, 1)
            self.assertFalse(Path(package["output"]).exists())

    def test_reference_mutation_after_preflight_stops_before_submission(self):
        self.image("ref.png")
        package = self.package(references=["ref.png"])
        self.image("ref.png", "red")
        with generate.RunState() as state, mock.patch.object(generate, "submit_prepared") as submit:
            with self.assertRaisesRegex(ValueError, "changed after preflight"):
                generate.execute_prepared("key", package, state)
            submit.assert_not_called()

    def test_native_and_external_share_identical_bound_prompt_and_reference_order(self):
        self.image("font.png")
        package = self.package(references=[{"path": "font.png", "role": "typography", "id": "T-title"}], prompt="使用[T-title]")
        with mock.patch.object(generate, "post_multipart", return_value=self.response()) as post:
            generate.submit_prepared("key", package)
        args = post.call_args.args
        self.assertEqual(dict(args[2])["prompt"], package["resolved_prompt"])
        self.assertEqual([ref[1] for ref in args[3]], [ref["path"] for ref in package["references"]])

    def test_apiyi_sends_explicit_quality_and_never_response_format(self):
        job = {"prompt": "Render the supplied slide text.", "output": "images/p01.png", "size": "32x16"}
        package = generate.prepare_job(job, channel="apiyi")
        self.assertEqual(package["model"], "gpt-image-2.6-sunburst")
        self.assertEqual(package["capabilities"]["quality"], "high")
        self.assertEqual(package["capabilities"]["max_references"], 16)
        with mock.patch.object(generate, "post_json", return_value=self.response()) as post:
            generate.submit_prepared("key", package)
        url, _, body = post.call_args.args
        self.assertEqual(url, "https://api.apiyi.com/v1/images/generations")
        self.assertEqual(body["quality"], "high")
        self.assertNotIn("response_format", body)

        self.image("font.png")
        edit = generate.prepare_job({**job, "references": [{"path": "font.png", "role": "typography", "id": "T-title"}],
                                     "prompt": "使用[T-title]"}, channel="apiyi")
        with mock.patch.object(generate, "post_multipart", return_value=self.response()) as post:
            generate.submit_prepared("key", edit)
        url, _, fields, files = post.call_args.args
        self.assertEqual(url, "https://api.apiyi.com/v1/images/edits")
        self.assertEqual(dict(fields)["quality"], "high")
        self.assertEqual([name for name, _ in files], ["image[]"])

    def test_apiyi_quality_override_changes_input_hash_but_other_channels_carry_no_quality(self):
        job = {"prompt": "Render the supplied slide text.", "output": "images/p01.png", "size": "32x16"}
        high = generate.prepare_job(job, channel="apiyi")
        with mock.patch.dict(os.environ, {"APIYI_IMAGE_QUALITY": "xhigh"}):
            xhigh = generate.prepare_job(job, channel="apiyi")
        self.assertEqual(xhigh["capabilities"]["quality"], "xhigh")
        self.assertNotEqual(high["input_hash"], xhigh["input_hash"])
        self.assertNotIn("quality", generate.prepare_job(job)["capabilities"])
        with mock.patch.object(generate, "post_json", return_value=self.response()) as post:
            generate.submit_prepared("key", generate.prepare_job(job))
        self.assertNotIn("quality", post.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
