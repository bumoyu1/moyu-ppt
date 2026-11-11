"""Offline regression tests for search mechanics, not visual-quality claims."""

import contextlib
import importlib.util
import io
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock


# Keep tests off any real per-user config or stored keys.
os.environ.setdefault("MOYU_PPT_CONFIG_DIR", tempfile.mkdtemp(prefix="ppt-config-test-"))
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pinterest_search.py"
SPEC = importlib.util.spec_from_file_location("inspiration_search_under_test", SCRIPT)
search = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(search)
try:
    from PIL import Image
except ImportError:
    Image = None


def tiny_png():
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", 2, 2, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00" + b"\xff\x00\x00" * 2 + b"\x00" + b"\x00\x00\xff" * 2)) + chunk(b"IEND", b"")


def pin(identity, image_url=None, source="https://example.test/template"):
    return {"id": identity, "image_url": image_url or f"https://i.pinimg.com/originals/{identity}.png", "source_link": source, "pinterest_url": f"https://www.pinterest.com/pin/{identity}/" if identity is not None else None}


def pool(count=5):
    return search.merge_candidates({"slides": [pin(str(i)) for i in range(count)]}, [{"query": "slides", "purpose": "layout"}], count)[0]


def style_plan():
    return {
        "brief": {
            "style_intent": "像一本当代游戏文化杂志，有探索感但保持清楚易读",
            "audience_use": "游戏行业读者，在屏幕上阅读故事和事实",
            "content_needs": ["人物故事", "时间线", "观点与证据"],
            "typography": "有性格的中文标题，正文清晰，数字便于比较",
            "avoid": ["用户不喜欢装饰拥挤的画面"],
            "style_terms": [],
            "exploration": "bounded",
            "scope": "directions",
            "decision": {"source": "user", "status": "confirmed", "evidence": "用户说：想像游戏文化杂志，有探索感，但画面别太满。"},
        },
        "queries": [{
            "query": "culture magazine presentation case study typography grid",
            "direction": "文化杂志",
            "purpose": "system",
            "target": "有标题、正文、人物与时间线的完整设计案例",
            "basis": "用户希望游戏文化杂志感；内容包含人物与时间线",
        }],
    }


class MergeTests(unittest.TestCase):
    def test_same_image_different_pin_ids_and_sizes_preserves_all_queries(self):
        results = {
            "editorial": [pin("a", "https://i.pinimg.com/736x/a/b/art.jpg?utm_source=feed", "https://EXAMPLE.test/template?utm_campaign=x&id=7#hero")],
            "data": [pin("b", "http://i.pinimg.com/originals/a/b/art.jpg", "https://example.test/template?id=7")],
        }
        plan = [{"query": "editorial", "direction": "type", "purpose": "layout"}, {"query": "data", "direction": "grid", "purpose": "data-layout"}]
        pins, stats = search.merge_candidates(results, plan, 20)
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["matched_queries"], ["data", "editorial"])
        self.assertEqual(pins[0]["pin_ids"], ["a", "b"])
        self.assertEqual(len(pins[0]["query_contexts"]), 2)
        self.assertEqual(pins[0]["canonical_source_links"], ["https://example.test/template?id=7"])
        self.assertEqual(stats["exact_duplicates_removed"], 1)

    def test_same_gallery_link_does_not_erase_different_layout_images(self):
        pins, _ = search.merge_candidates({"slides": [pin("cover"), pin("data")]}, [], 20)
        self.assertEqual(len(pins), 2)
        self.assertEqual(pins[0]["canonical_source_links"], pins[1]["canonical_source_links"])

    def test_missing_pin_ids_do_not_collapse_unrelated_images(self):
        pins, _ = search.merge_candidates({"q": [pin(None, "https://example.test/one.png"), pin(None, "https://example.test/two.png")]}, [], 20)
        self.assertEqual(len(pins), 2)
        self.assertNotEqual(pins[0]["candidate_id"], pins[1]["candidate_id"])

    def test_query_and_completion_order_do_not_change_candidate_identity_or_mapping(self):
        first = {"z": [pin("shared"), pin("z")], "a": [pin("alias", "https://i.pinimg.com/736x/shared.png"), pin("a")]}
        second = {"a": first["a"], "z": first["z"]}
        plan = [{"query": q, "purpose": "layout"} for q in first]
        one, _ = search.merge_candidates(first, plan, 20)
        two, _ = search.merge_candidates(second, list(reversed(plan)), 20)
        self.assertEqual(one, two)
        self.assertEqual([p["candidate_index"] for p in one], [1, 2, 3])

    def test_functional_cdn_query_parameters_are_not_discarded(self):
        pins, _ = search.merge_candidates({"q": [pin("a", "https://cdn.test/image?asset=1&utm_source=x"), pin("b", "https://cdn.test/image?asset=2")]}, [], 20)
        self.assertEqual(len(pins), 2)

    def test_identity_alias_bridge_keeps_all_provenance(self):
        rows = {"a": [pin("same-pin", "https://cdn.test/1.png")], "b": [pin("same-pin", "https://cdn.test/2.png")], "c": [pin("repost", "https://cdn.test/2.png")]}
        pins, _ = search.merge_candidates(rows, [], 20)
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["matched_queries"], ["a", "b", "c"])


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_failure_refills_from_same_pool_and_maps_actual_successes(self):
        pins = pool(5)
        attempted = []
        def download(candidate, dest, index):
            attempted.append(candidate["id"])
            if candidate["id"] == "1":
                raise OSError("fixture failure")
            path = dest / (candidate["candidate_id"] + ".png")
            path.write_bytes(tiny_png())
            return {"candidate_id": candidate["candidate_id"], "local_path": str(path), "preview_validation": "decoded" if Image else "unverified_no_pillow"}
        with mock.patch.object(search, "_download_one", side_effect=download), mock.patch.object(search, "search_pinterest", side_effect=AssertionError("no extra search")):
            search.download_previews(pins, str(self.directory), 3, 3)
        ready = [p for p in pins if p.get("local_path")]
        self.assertEqual([p["id"] for p in ready], ["0", "2", "3"])
        self.assertEqual([p["review_index"] for p in ready], [1, 2, 3])
        self.assertEqual(pins[1]["download_status"], "failed")
        self.assertIsNone(pins[1]["review_index"])
        self.assertNotIn("4", attempted)
        self.assertTrue(all(p["display_index"] is None for p in pins))

    def test_no_network_retry_beyond_pool_when_every_preview_fails(self):
        pins = pool(3)
        with mock.patch.object(search, "_download_one", side_effect=OSError("offline")) as download:
            results = search.download_previews(pins, str(self.directory), 10, 2)
        self.assertEqual(download.call_count, 3)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(p["download_status"] == "failed" for p in pins))

    @unittest.skipIf(Image is None, "Pillow unavailable; decode-specific check cannot run")
    def test_invalid_preview_bytes_trigger_original_fallback(self):
        candidate = pool(1)[0]
        with mock.patch.object(search.urllib.request, "urlopen", side_effect=[io.BytesIO(b"<html>not an image</html>"), io.BytesIO(tiny_png())]) as request:
            result = search._download_one(candidate, self.directory, 1)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["preview_validation"], "decoded")
        self.assertIn(candidate["candidate_id"], Path(result["local_path"]).name)

    @unittest.skipIf(Image is None, "Pillow unavailable; contact-sheet check cannot run")
    def test_sheet_uses_json_index_and_candidate_id_after_missing_preview(self):
        pins = pool(3)
        for index, candidate in enumerate((pins[0], pins[2]), 1):
            path = self.directory / (candidate["candidate_id"] + ".png")
            path.write_bytes(tiny_png())
            candidate.update(local_path=str(path), review_index=index)
        sheet_path = self.directory / "sheet.jpg"
        with mock.patch("PIL.ImageDraw.ImageDraw.text", autospec=True) as text_draw:
            search.build_contact_sheet(pins, str(sheet_path))
        labels = [call.args[2] for call in text_draw.call_args_list]
        self.assertEqual(labels, [f"[1] {pins[0]['candidate_id']}", f"[2] {pins[2]['candidate_id']}"])
        self.assertTrue(sheet_path.is_file())


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def run_main(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = search.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_query_plan_keeps_multiple_roles_for_same_query_without_duplicate_requests(self):
        plan = self.directory / "plan.json"
        plan.write_text(json.dumps({"queries": [{"query": "slides", "purpose": "layout"}, {"query": "slides", "purpose": "type"}]}))
        payload = {"success": True, "pins": [{"id": "p", "images": {"orig": {"url": "https://i.pinimg.com/originals/p.png"}}}]}
        with mock.patch.object(search, "load_api_key", return_value="LOCAL_TEST_ONLY"), mock.patch.object(search, "search_pinterest", return_value=payload) as provider:
            code, out, err = self.run_main(["--query-plan", str(plan)])
        result = json.loads(out)
        self.assertEqual(code, 0, err)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(len(result["pins"][0]["query_contexts"]), 2)
        self.assertEqual(result["display_candidates"], [])
        self.assertEqual(result["visual_diversity"], "not_automatically_assessed")

    def test_legacy_flags_report_actual_shortfall_not_nominal_candidate_count(self):
        payload = {"success": True, "pins": [{"id": str(i), "images": {"orig": {"url": f"https://i.pinimg.com/originals/{i}.png"}}} for i in range(3)]}
        def download(candidate, dest, index):
            if candidate["id"] != "2":
                raise OSError("fixture unavailable")
            path = dest / "usable.png"
            path.write_bytes(tiny_png())
            return {"local_path": str(path), "preview_validation": "unverified_no_pillow"}
        with mock.patch.object(search, "load_api_key", return_value="LOCAL_TEST_ONLY"), mock.patch.object(search, "search_pinterest", return_value=payload), mock.patch.object(search, "_download_one", side_effect=download):
            code, out, err = self.run_main(["slides", "--download-count", "2", "--download-dir", str(self.directory)])
        result = json.loads(out)
        self.assertEqual(code, 0, err)
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["review_preview_count"], 1)
        self.assertEqual(result["decoded_preview_count"], 0)
        self.assertEqual(result["download_summary"]["shortfall_count"], 1)
        self.assertEqual(result["download_summary"]["attempted_count"], 3)
        self.assertEqual(result["display_count"], 0)

    def test_explicit_local_selection_creates_separate_ordered_display_pool(self):
        pins = pool(3)
        for index, candidate in enumerate(pins, 1):
            path = self.directory / (candidate["candidate_id"] + ".png")
            path.write_bytes(tiny_png())
            candidate.update(local_path=str(path), review_index=index)
        input_path = self.directory / "refs.json"
        input_path.write_text(json.dumps({"pins": pins, "review_candidates": [search.pool_entry(p, "review_index") for p in pins]}))
        ids = [pins[2]["candidate_id"], pins[0]["candidate_id"]]
        with mock.patch.object(search, "load_api_key", side_effect=AssertionError("must not read key")), mock.patch.object(search.urllib.request, "urlopen", side_effect=AssertionError("must stay local")):
            code, out, err = self.run_main(["--input", str(input_path), "--select-candidate", ids[0], "--select-candidate", ids[1]])
        result = json.loads(out)
        self.assertEqual(code, 0, err)
        self.assertEqual([p["candidate_id"] for p in result["display_candidates"]], ids)
        self.assertEqual([p["display_index"] for p in result["display_candidates"]], [1, 2])
        self.assertEqual(len(result["review_candidates"]), 3)
        self.assertEqual(result["pins"][1]["display_index"], None)

    def test_local_selection_rejects_missing_preview_and_unknown_ids(self):
        pins = pool(1)
        input_path = self.directory / "refs.json"
        input_path.write_text(json.dumps({"pins": pins}))
        with mock.patch.object(search, "load_api_key", side_effect=AssertionError("must not read key")):
            for identity in ("unknown", pins[0]["candidate_id"]):
                code, out, err = self.run_main(["--input", str(input_path), "--select-candidate", identity])
                self.assertEqual(code, 2)
                self.assertFalse(out)
                self.assertIn("Local selection failed", err)

    def test_invalid_query_plan_and_worker_count_fail_before_key_read(self):
        bad_plan = self.directory / "bad.json"
        bad_plan.write_text('{"queries": [{"query": "", "purpose": "layout"}]}')
        with mock.patch.object(search, "load_api_key", side_effect=AssertionError("must not read key")):
            for args in (["--query-plan", str(bad_plan)], ["slides", "--workers", "0"]):
                code, out, err = self.run_main(args)
                self.assertEqual(code, 2)
                self.assertFalse(out)

    def test_provider_error_never_echoes_configured_key(self):
        with mock.patch.object(search, "load_api_key", return_value="PRIVATE_FIXTURE_KEY"), mock.patch.object(search, "search_pinterest", side_effect=search.PinterestSearchError("provider reflected PRIVATE_FIXTURE_KEY")):
            code, out, err = self.run_main(["slides"])
        self.assertEqual(code, 1)
        self.assertNotIn("PRIVATE_FIXTURE_KEY", out + err)
        self.assertIn("[redacted]", err)


class SearchBriefTests(unittest.TestCase):
    """Preflight checks records and coverage; it does not rate visual quality."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.plan_path = self.directory / "plan.json"

    def save_plan(self, payload):
        self.plan_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(self.plan_path)

    def run_main(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = search.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_unanswered_or_empty_evidence_stops_before_keys_network_and_writes(self):
        for change in ({"status": "pending"}, {"evidence": " "}, {"source": "assistant"}):
            with self.subTest(change=change):
                plan = style_plan()
                plan["brief"]["decision"].update(change)
                output = self.directory / "new-output" / "refs.json"
                previews = self.directory / "new-previews"
                with mock.patch.object(search, "load_api_key", side_effect=AssertionError("must not read key")), mock.patch.object(search.urllib.request, "urlopen", side_effect=AssertionError("must not access network")):
                    code, out, err = self.run_main(["--query-plan", self.save_plan(plan), "--require-brief", "--output", str(output), "--download-dir", str(previews)])
                self.assertEqual(code, 2, err)
                self.assertFalse(out)
                self.assertIn("brief.decision", err)
                self.assertFalse(output.parent.exists())
                self.assertFalse(previews.exists())

    def test_legacy_query_plan_is_supported_but_not_a_strict_brief(self):
        path = self.save_plan({"queries": [{"query": "fantasy game concept art presentation dark gold"}]})
        with mock.patch.object(search, "load_api_key", side_effect=AssertionError("must not read key")):
            code, out, err = self.run_main(["--query-plan", path, "--require-brief"])
        self.assertEqual(code, 2)
        self.assertIn("brief", err)
        with mock.patch.object(search, "load_api_key", return_value="LOCAL_TEST_ONLY"), mock.patch.object(search, "search_pinterest", return_value={"success": True, "pins": []}) as provider:
            code, out, err = self.run_main(["--query-plan", path])
        self.assertEqual(code, 0, err)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(json.loads(out)["query_plan"], [{"query": "fantasy game concept art presentation dark gold"}])

    def test_strict_mode_rejects_unplanned_positional_query(self):
        with mock.patch.object(search, "load_api_key", side_effect=AssertionError("must not read key")):
            code, out, err = self.run_main(["extra unplanned art", "--query-plan", self.save_plan(style_plan()), "--require-brief"])
        self.assertEqual(code, 2)
        self.assertIn("positional queries", err)

    def test_plan_without_layout_keywords_is_refused(self):
        plan = style_plan()
        plan["queries"][0].update(query="fantasy game landscape concept art", purpose="image-language")
        with mock.patch.object(search, "load_api_key", side_effect=AssertionError("must not read key")):
            code, out, err = self.run_main(["--query-plan", self.save_plan(plan), "--require-brief"])
        self.assertEqual(code, 2)
        self.assertIn("The plan has no layout keywords", err)

    def test_layout_and_style_element_keywords_live_in_separate_groups(self):
        plan = style_plan()
        # Pictures of the look itself need no presentation word and form a group of their own.
        plan["queries"].append({**plan["queries"][0], "query": "expressive colorful sculpture", "direction": "材质与图像", "purpose": "material"})
        code, out, err = self.run_main(["--query-plan", self.save_plan(plan), "--require-brief", "--validate-plan"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["directions"], ["文化杂志", "材质与图像"])
        mixed = style_plan()
        mixed["queries"].append({**mixed["queries"][0], "query": "expressive colorful sculpture", "purpose": "material"})
        with mock.patch.object(search, "load_api_key", side_effect=AssertionError("must not read key")):
            code, out, err = self.run_main(["--query-plan", self.save_plan(mixed), "--require-brief"])
        self.assertEqual(code, 2)
        self.assertIn("in separate groups, so the user can pick from each: 文化杂志", err)
        # A layout keyword that would return posters or spreads instead of slides is refused.
        loose = style_plan()
        loose["queries"].append({**loose["queries"][0], "query": "expressive magazine information spread", "direction": "版式", "purpose": "layout"})
        code, out, err = self.run_main(["--query-plan", self.save_plan(loose), "--require-brief", "--validate-plan"])
        self.assertEqual(code, 2)
        self.assertIn("is a layout keyword without a presentation word", json.loads(out)["error"])

    def test_open_exploration_with_user_permission_can_validate_without_forced_style(self):
        plan = style_plan()
        plan["brief"].update(style_intent="还没有偏好，先对比不同设计思路", avoid=[], exploration="open")
        plan["brief"]["decision"]["evidence"] = "用户说：我没想法，你先给我几个不同风格看看。"
        code, out, err = self.run_main(["--query-plan", self.save_plan(plan), "--require-brief", "--validate-plan"])
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["valid"])

    def named_style_plan(self):
        """A named style: its own decks, its layouts by page type, and pictures of the style."""
        plan = style_plan()
        plan["brief"].update(style_intent="像素风的培训课件", style_terms=["pixel art", "像素风", "ドット絵"])
        first = plan["queries"][0]
        plan["queries"] = [{**first, "query": "Pixel  Art presentation slides", "direction": "成套演示", "purpose": "system"},
                           {**first, "query": "像素风 PPT 模板", "direction": "成套演示", "purpose": "system"},
                           {**first, "query": "ドット絵 スライド デザイン", "direction": "成套演示", "purpose": "system"},
                           {**first, "query": "pixel art infographic presentation slide", "direction": "风格里的版式·信息图", "purpose": "layout"},
                           {**first, "query": "pixel art game interface icons", "direction": "风格元素·界面与图标", "purpose": "image-language"}]
        return plan

    def test_named_style_keeps_every_keyword_on_it_unless_the_user_asked_for_more(self):
        plan = self.named_style_plan()
        first = plan["queries"][0]
        code, out, err = self.run_main(["--query-plan", self.save_plan(plan), "--require-brief", "--validate-plan"])
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["directions"], ["成套演示", "风格元素·界面与图标", "风格里的版式·信息图"])
        # A look-alike rendering the agent thought of is not the style the user named: not as a deck...
        plan["queries"].append({**first, "query": "voxel isometric presentation design", "direction": "成套演示", "purpose": "system"})
        with mock.patch.object(search, "load_api_key", side_effect=AssertionError("must not read key")):
            code, out, err = self.run_main(["--query-plan", self.save_plan(plan), "--require-brief"])
        self.assertEqual(code, 2)
        self.assertIn('"voxel isometric presentation design" leaves the style the user named (pixel art / 像素风 / ドット絵)', err)
        # ...not as a style element...
        element = self.named_style_plan()
        element["queries"].append({**first, "query": "voxel game interface", "direction": "风格元素·界面与图标", "purpose": "image-language"})
        code, out, err = self.run_main(["--query-plan", self.save_plan(element), "--require-brief", "--validate-plan"])
        self.assertEqual(code, 2)
        self.assertIn('"voxel game interface" leaves the style the user named', json.loads(out)["error"])
        # ...and a generic business layout has nothing to do with it either.
        generic = self.named_style_plan()
        generic["queries"].append({**first, "query": "process flow comparison presentation slide layout", "direction": "通用版式", "purpose": "layout"})
        code, out, err = self.run_main(["--query-plan", self.save_plan(generic), "--require-brief", "--validate-plan"])
        self.assertEqual(code, 2)
        self.assertIn("Generic layouts without the style's name", json.loads(out)["error"])
        generic["queries"][-1]["beyond_style"] = "用户说：流程图的版式也搜几张来对比"
        code, out, err = self.run_main(["--query-plan", self.save_plan(generic), "--require-brief", "--validate-plan"])
        self.assertEqual(code, 0, out)
        plan["queries"][-1]["beyond_style"] = " "
        code, out, err = self.run_main(["--query-plan", self.save_plan(plan), "--require-brief", "--validate-plan"])
        self.assertEqual(code, 2)
        self.assertIn("beyond_style must quote the user's words", json.loads(out)["error"])
        plan["queries"][-1]["beyond_style"] = "用户说：体素的也搜来看看"
        path = self.save_plan(plan)
        code, out, err = self.run_main(["--query-plan", path, "--require-brief", "--validate-plan"])
        self.assertEqual(code, 0, out)
        entries, _ = search.load_search_plan([], path, require_brief=True)
        self.assertEqual(entries[-1]["beyond_style"], "用户说：体素的也搜来看看")
        self.assertNotIn("beyond_style", entries[0])

    def test_named_style_needs_its_slides_and_its_own_elements(self):
        plan = self.named_style_plan()
        plan["queries"] = [q for q in plan["queries"] if q["purpose"] == "image-language"]
        code, out, err = self.run_main(["--query-plan", self.save_plan(plan), "--require-brief", "--validate-plan"])
        self.assertEqual(code, 2)
        self.assertIn("The plan has no layout keywords", json.loads(out)["error"])
        plan = self.named_style_plan()
        plan["queries"] = [q for q in plan["queries"] if q["purpose"] != "image-language"]
        code, out, err = self.run_main(["--query-plan", self.save_plan(plan), "--require-brief", "--validate-plan"])
        self.assertEqual(code, 2)
        self.assertIn("A named style needs style-element groups", json.loads(out)["error"])

    def test_style_terms_must_be_stated_even_when_the_user_named_no_style(self):
        plan = style_plan()
        plan["brief"].pop("style_terms")
        code, out, err = self.run_main(["--query-plan", self.save_plan(plan), "--require-brief", "--validate-plan"])
        self.assertEqual(code, 2)
        self.assertIn("brief.style_terms must list the names", json.loads(out)["error"])
        plan["brief"]["style_terms"] = ["", "像素风"]
        code, out, err = self.run_main(["--query-plan", self.save_plan(plan), "--require-brief", "--validate-plan"])
        self.assertEqual(code, 2)

    def test_typography_supplement_requires_an_existing_fixed_style(self):
        plan = style_plan()
        plan["brief"].update(scope="supplement", existing_style="已选定的文化杂志系统，保留网格、色彩和页面节奏")
        plan["queries"][0].update(query="Chinese editorial headline body text pairing", purpose="typography")
        code, out, err = self.run_main(["--query-plan", self.save_plan(plan), "--require-brief", "--validate-plan"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["scope"], "supplement")
        del plan["brief"]["existing_style"]
        with mock.patch.object(search, "load_api_key", side_effect=AssertionError("must not read key")):
            code, out, err = self.run_main(["--query-plan", self.save_plan(plan), "--require-brief"])
        self.assertEqual(code, 2)
        self.assertIn("brief.existing_style", err)

    def test_purpose_target_and_basis_cannot_be_omitted_from_strict_queries(self):
        for key in ("direction", "purpose", "target", "basis"):
            with self.subTest(key=key):
                plan = style_plan()
                del plan["queries"][0][key]
                with mock.patch.object(search, "load_api_key", side_effect=AssertionError("must not read key")):
                    code, out, err = self.run_main(["--query-plan", self.save_plan(plan), "--require-brief"])
                self.assertEqual(code, 2)
                self.assertIn(f"Query 1.{key}", err)

    def test_brief_and_all_query_contexts_survive_retrieval_dedup_and_local_selection(self):
        plan = style_plan()
        plan["queries"].append({**plan["queries"][0], "query": "Chinese editorial typography spread", "direction": "中文字形", "purpose": "typography", "target": "同系统中的中文标题和正文关系", "basis": "用户希望有性格的标题和清晰正文"})
        payload = {"success": True, "pins": [{"id": "shared", "images": {"orig": {"url": "https://i.pinimg.com/originals/shared.png"}}}]}
        def download(candidate, dest, index):
            path = dest / (candidate["candidate_id"] + ".png")
            path.write_bytes(tiny_png())
            return {"local_path": str(path), "preview_validation": "decoded" if Image else "unverified_no_pillow"}
        with mock.patch.object(search, "load_api_key", return_value="LOCAL_TEST_ONLY"), mock.patch.object(search, "search_pinterest", return_value=payload) as provider, mock.patch.object(search, "_download_one", side_effect=download):
            code, out, err = self.run_main(["--query-plan", self.save_plan(plan), "--require-brief", "--download-dir", str(self.directory / "previews")])
        self.assertEqual(code, 0, err)
        self.assertEqual(provider.call_count, 2)
        result = json.loads(out)
        self.assertEqual(result["brief"], plan["brief"])
        self.assertEqual(result["query_plan"], plan["queries"])
        self.assertEqual(result["count"], 1)
        self.assertCountEqual(result["pins"][0]["query_contexts"], plan["queries"])
        refs = self.directory / "refs.json"
        refs.write_text(out, encoding="utf-8")
        with mock.patch.object(search, "load_api_key", side_effect=AssertionError("must stay local")), mock.patch.object(search.urllib.request, "urlopen", side_effect=AssertionError("must stay local")):
            code, out, err = self.run_main(["--input", str(refs), "--require-brief", "--select-candidate", result["pins"][0]["candidate_id"]])
        self.assertEqual(code, 0, err)
        selected = json.loads(out)
        self.assertEqual(selected["brief"], plan["brief"])
        self.assertEqual(selected["query_plan"], plan["queries"])
        self.assertEqual(selected["pins"][0]["query_contexts"], result["pins"][0]["query_contexts"])

    def test_strict_dry_run_never_reads_credentials_uses_network_or_writes(self):
        output = self.directory / "not-created" / "refs.json"
        previews = self.directory / "not-created-previews"
        with mock.patch.object(search, "load_api_key", side_effect=AssertionError("must not read key")), mock.patch.object(search.urllib.request, "urlopen", side_effect=AssertionError("must not access network")), mock.patch.object(search, "write_result", side_effect=AssertionError("must not write")):
            code, out, err = self.run_main(["--query-plan", self.save_plan(style_plan()), "--require-brief", "--validate-plan", "--output", str(output), "--download-dir", str(previews), "--contact-sheet"])
        self.assertEqual(code, 0, err)
        diagnostic = json.loads(out)
        self.assertTrue(diagnostic["valid"])
        self.assertTrue(diagnostic["strict_brief"])
        self.assertIn("structure_only", diagnostic["assessment"])
        self.assertFalse(output.parent.exists())
        self.assertFalse(previews.exists())

    def test_invalid_dry_run_is_json_and_cannot_enter_original_download(self):
        plan = style_plan()
        plan["brief"]["decision"]["evidence"] = ""
        with mock.patch.object(search, "load_api_key", side_effect=AssertionError("must not read key")), mock.patch.object(search, "download_original", side_effect=AssertionError("must not download")):
            for extra in ([], ["--download-original", "https://example.test/a.png", "--output", str(self.directory / "a.png")]):
                code, out, err = self.run_main(["--query-plan", self.save_plan(plan), "--require-brief", "--validate-plan", *extra])
                self.assertEqual(code, 2)
                self.assertFalse(json.loads(out)["valid"])

    def test_original_download_remains_independent_of_new_search_brief(self):
        destination = str(self.directory / "original.png")
        with mock.patch.object(search, "load_api_key", side_effect=AssertionError("must not read key")), mock.patch.object(search, "download_original", return_value={"local_path": destination}) as download:
            code, out, err = self.run_main(["--download-original", "https://example.test/original.png", "--output", destination, "--require-brief"])
        self.assertEqual(code, 0, err)
        download.assert_called_once_with("https://example.test/original.png", destination)


if __name__ == "__main__":
    unittest.main()
