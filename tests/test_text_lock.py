"""Text-lock regressions: 字框 arithmetic, the character budget, measurement on
synthetic slides, the drift feedback loop and the delivery gate. No network."""
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageChops, ImageDraw, ImageFont

os.environ.setdefault("MOYU_PPT_CONFIG_DIR", tempfile.mkdtemp(prefix="ppt-config-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import deck_delivery as delivery
import deck_runtime
import extract_briefs as extract
import text_lock

CANVAS = (1536, 864)
MINT = (234, 244, 241)
INK = (35, 43, 51)
TITLE = "- 字号锁 标题：字高 6.5%，顶 7%，左 4.6%，最宽 58%"
SUB = "- 字号锁 章节小标题：字高 11%，顶 35%，左 4.6%，最宽 40%，字宽比 0.85"
MAIN = "- 字号锁 章节主标题：字高 17.4%，顶 47.7%，左 4.6%，最宽 42%，字宽比 0.85"


def make_lock(line, text, aspect=16 / 9):
    name, spec = next(iter(text_lock.parse_lock_groups(line).items()))
    box = text_lock.lock_box(spec, text, aspect)
    return {"text": text, "group": name, **spec, "units": box["units"],
            "char_width_pct": box["char_width_pct"], "box_pct": box["box_pct"]}


def draw_line(image, lock, scale=1.0, dx=0, dy=0, ratio=1.05):
    """One dark square per character, laid out the way a locked line should be."""
    x0, y0, _, y1 = (lock["box_pct"][0] * CANVAS[0] / 100, lock["box_pct"][1] * CANVAS[1] / 100,
                     0, lock["box_pct"][3] * CANVAS[1] / 100)
    height = round((y1 - y0) * scale)
    advance = height * lock.get("width_ratio", ratio)
    draw = ImageDraw.Draw(image)
    left, top = round(x0) + dx, round(y0) + dy
    for index in range(int(lock["units"])):
        cell = left + round(index * advance)
        draw.rectangle([cell, top, cell + round(height * 0.92), top + height - 1], fill=INK)


def slide():
    return Image.new("RGB", CANVAS, MINT)


class PlanSyntaxTests(unittest.TestCase):
    def test_group_line_reads_size_position_budget_and_ratio(self):
        groups = text_lock.parse_lock_groups("画布说明。\n" + MAIN + "。\n" + TITLE + "\n")
        self.assertEqual(groups["章节主标题"], {"ink_height_pct": 17.4, "top_pct": 47.7, "left_pct": 4.6,
                                           "max_width_pct": 42.0, "width_ratio": 0.85})
        self.assertNotIn("width_ratio", groups["标题"])

    def test_malformed_groups_fail_with_the_group_name(self):
        invalid = {
            "no_height": "- 字号锁 标题：顶 7%，左 4.6%",
            "no_top": "- 字号锁 标题：字高 6%，左 4.6%",
            "no_anchor": "- 字号锁 标题：字高 6%，顶 7%",
            "two_anchors": "- 字号锁 标题：字高 6%，顶 7%，左 4%，居中 50%",
            "bare_number": "- 字号锁 标题：字高 6，顶 7%，左 4%",
            "unknown_part": "- 字号锁 标题：字高 6%，顶 7%，左 4%，加粗",
            "ratio_range": "- 字号锁 标题：字高 6%，顶 7%，左 4%，字宽比 3",
            "leaves_canvas": "- 字号锁 标题：字高 20%，顶 90%，左 4%",
            "duplicate": TITLE + "\n" + TITLE,
        }
        for reason, text in invalid.items():
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, "标题"):
                text_lock.parse_lock_groups(text)

    def test_units_count_full_and_half_width_characters(self):
        self.assertEqual(text_lock.text_units("冲煮方案"), 4)
        self.assertEqual(text_lock.text_units("澄清：确保"), 5)
        self.assertEqual(text_lock.text_units("100%"), 2.2)

    def test_box_height_is_fixed_and_width_follows_the_count(self):
        spec = text_lock.parse_lock_groups(TITLE)["标题"]
        short, long = text_lock.lock_box(spec, "六个字的标题", 16 / 9), text_lock.lock_box(spec, "十二个字的标题就有这么长", 16 / 9)
        self.assertEqual((short["box_pct"][1], short["box_pct"][3]), (long["box_pct"][1], long["box_pct"][3]))
        self.assertAlmostEqual((long["box_pct"][2] - long["box_pct"][0]) / (short["box_pct"][2] - short["box_pct"][0]), 2, places=2)
        self.assertEqual(short["char_width_pct"], long["char_width_pct"])
        # A wider canvas makes the same ink height a smaller share of the width.
        self.assertLess(text_lock.lock_box(spec, "六个字的标题", 21 / 9)["char_width_pct"], short["char_width_pct"])

    def test_budget_rejects_a_line_that_cannot_fit_and_names_the_limit(self):
        groups = text_lock.parse_lock_groups(TITLE)
        fits = "**可见文字清单**：\n可见文字=冲煮者的观念转变；位置=左上标题；角色=title；锁=标题\n"
        self.assertEqual(text_lock.page_locks(fits, groups, 5)[0]["units"], 8)
        long = fits.replace("冲煮者的观念转变", "老师傅告诉我们新手入门之后一定要经历的四个阶段")
        with self.assertRaisesRegex(ValueError, r"P5: .*shorten it to about 15 个字"):
            text_lock.page_locks(long, groups, 5)

    def test_locked_lines_must_be_declared_single_and_unique_per_page(self):
        groups = text_lock.parse_lock_groups(TITLE)
        head = "**可见文字清单**：\n"
        with self.assertRaisesRegex(ValueError, "not declared"):
            text_lock.page_locks(head + "可见文字=标题；位置=左上；锁=封面\n", groups, 1)
        with self.assertRaisesRegex(ValueError, "one visual line"):
            text_lock.page_locks(head + "可见文字=咖啡豆子与器具；位置=左区，分两行；锁=标题\n", groups, 1)
        with self.assertRaisesRegex(ValueError, "only one line per page"):
            text_lock.page_locks(head + "可见文字=标题一；位置=上；锁=标题\n可见文字=标题二；位置=下；锁=标题\n", groups, 1)
        self.assertEqual(text_lock.page_locks(head + "可见文字=没有锁；位置=中央\n", groups, 1), [])

    def test_appendix_states_the_box_and_only_matching_feedback(self):
        lock = make_lock(TITLE, "冲煮的基本逻辑")
        text = text_lock.brief_appendix([lock], [{"text": "冲煮的基本逻辑", "group": "标题", "note": "上次画大了。"},
                                                 {"text": "别的标题", "group": "标题", "note": "不相关。"}])
        self.assertIn("字框横向 4.6%–31.5%，纵向 7%–13.5%", text)
        self.assertIn("固定墨迹字高", text)
        self.assertIn("保留字形比例与自然字距", text)
        self.assertIn("横向字框只用于布局预算", text)
        self.assertNotIn("正好填满字框", text)
        self.assertNotIn("右端到字框右边", text)
        self.assertIn("一行约放 26 个字", text)
        self.assertIn("上次画大了。", text)
        self.assertNotIn("不相关", text)
        self.assertNotIn("可见文字=", text)  # must never be read back as page copy
        self.assertEqual(text_lock.brief_appendix([]), "")

    def test_size_guide_does_not_transfer_native_glyph_style(self):
        lock = {**make_lock(TITLE, "冲煮的基本逻辑"), "template": "geometry.png"}
        text = text_lock.brief_appendix([lock], template_ids={"geometry.png": "SZ1"})
        self.assertIn("字号样板 [SZ1]", text)
        self.assertIn("不从样板迁移字形、字距或字宽", text)
        self.assertIn("实际字形沿用目标页或指定字形参考", text)


class InDeck(unittest.TestCase):
    """Fictional confirmed deck state, for contract tests only."""

    constants = "## 全篇常量块\n画布为PPT，文字必须准确。\n" + TITLE + "\n"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppt-text-lock-test-")
        self.previous = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, self.previous)
        self.addCleanup(self.temp.cleanup)
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

    @staticmethod
    def page(number, title, anchor=None, lock="；锁=标题"):
        note = "本页为族锚定页" if anchor is None else f"锚定页 P{anchor}"
        return (f"## 第{number}页设计方案\n**[场景族]**：日光（{note}）\n**[空间定位图]**\n构图与内容。\n"
                f"**可见文字清单**：\n可见文字={title}；位置=左上标题；角色=title{lock}\n可见文字=正文一句；位置=中部\n")

    def extraction(self, content, *args):
        Path("plans.md").write_text(content)
        self.workflow["brief"]["page_count"] = max(number for number, _ in extract.split_pages(content))
        Path("deck-state.json").write_text(json.dumps({"workflow": self.workflow}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()) as err:
            code = extract.main(["plans.md", *args])
        return code, out.getvalue(), err.getvalue()


class ExtractionTests(InDeck):
    def test_jobs_and_briefs_carry_one_height_and_count_based_widths(self):
        constants = self.constants.replace("最宽 58%", "最宽 58%，字数 4–8")
        code, out, _ = self.extraction(constants + self.page(1, "四字标题") + self.page(2, "八个字的页面标题", anchor=1))
        self.assertEqual(code, 0)
        first = json.loads(Path("manifest-anchors.json").read_text())[0]["text_locks"][0]
        second = json.loads(Path("manifest-pages.json").read_text())[0]["text_locks"][0]
        self.assertEqual((first["box_pct"][1], first["box_pct"][3]), (second["box_pct"][1], second["box_pct"][3]))
        self.assertAlmostEqual(second["box_pct"][2] - second["box_pct"][0], 2 * (first["box_pct"][2] - first["box_pct"][0]), places=2)
        brief = Path("briefs/p02.txt").read_text()
        self.assertIn("字号锁（尺寸指令", brief)
        self.assertIn("“八个字的页面标题”（8 个字宽）", brief)
        self.assertEqual(delivery.expected_copy({"prompt_file": "briefs/p02.txt"}), ["八个字的页面标题", "正文一句"])
        report = json.loads(out)
        self.assertEqual(report["text_locks"]["标题"], {"pages": [1, 2], "min_units": 4, "max_units": 8})
        self.assertTrue(any("还没有字号样板" in warning for warning in report["warnings"]))
        self.assertNotIn("字数", Path("deck-constants.txt").read_text())  # a planning field, not a drawing instruction

    def test_pages_without_locks_get_the_brief_and_the_copy_reading_note_only(self):
        code, out, _ = self.extraction("## 全篇常量块\n画布为PPT。\n" + self.page(1, "标题", lock=""))
        self.assertEqual(code, 0)
        self.assertNotIn("text_locks", json.loads(Path("manifest-anchors.json").read_text())[0])
        brief = Path("briefs/p01.txt").read_text()
        self.assertEqual(brief, "构图与内容。\n**可见文字清单**：\n可见文字=标题；位置=左上标题；角色=title\n可见文字=正文一句；位置=中部"
                                + extract.COPY_READING_NOTE + "\n")
        # The field separator must never be drawn as part of a line.
        self.assertIn("分号本身和它后面的“位置=”“角色=”“锁=”是给排版看的说明，不画进画面", brief)
        self.assertNotIn("字号锁", brief)
        self.assertEqual(json.loads(out)["warnings"], [])

    def sourced(self, number, title, anchor=None, lock="；锁=标题", spot="左上标题", source=None):
        """A page with a [内容来源], so the split knows whether it is a content page."""
        source = source or f"A{number}"
        return self.page(number, title, anchor, lock).replace(
            "**[空间定位图]**", f"**[内容来源]**：{source}\n**[空间定位图]**").replace("位置=左上标题", f"位置={spot}")

    def choose(self, layout):
        self.workflow["title_layout_rules"] = True
        self.workflow["brief"]["title_layout"] = layout

    def test_uniform_layout_needs_locked_content_titles(self):
        self.choose("uniform")
        plan = "## 全篇常量块\n画布为PPT。\n" + self.sourced(1, "四字标题", lock="") + self.sourced(2, "另一标题", anchor=1, lock="")
        code, _, err = self.extraction(plan)
        self.assertEqual(code, 1)
        self.assertIn("统一版式", err)
        self.assertIn("锁=标题", err)
        constants = self.constants.replace("最宽 58%", "最宽 58%，字数 4–4")
        code, out, _ = self.extraction(constants + self.sourced(1, "四字标题") + self.sourced(2, "另一标题", anchor=1)
                                       + self.sourced(3, "特别的页", anchor=1, lock=""))
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["title_layout"], "uniform")
        self.assertTrue(any("P3 的标题没有字号锁" in warning for warning in report["warnings"]))
        self.assertNotIn("标题的样子", Path("briefs/p02.txt").read_text())

    def test_free_layout_rejects_locked_content_titles(self):
        self.choose("free")
        code, _, err = self.extraction(self.constants + self.sourced(1, "四字标题") + self.sourced(2, "另一标题", anchor=1))
        self.assertEqual(code, 1)
        self.assertIn("自由构图", err)
        self.assertIn("P1、P2", err)

    def test_free_layout_adds_one_plain_sentence_and_no_size_lock(self):
        self.choose("free")
        spots = ["封面大字", "左上", "右侧竖排", "压在主体上", "底部横幅"]
        sources = ["封面", "A1", "A2", "A3", "A4"]
        plan = "## 全篇常量块\n画布为PPT。\n" + "".join(
            self.sourced(n + 1, f"标题{n}", anchor=None if n == 0 else 1, lock="", spot=spots[n], source=sources[n]) for n in range(5))
        code, out, _ = self.extraction(plan)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["warnings"], [])
        content, cover = Path("briefs/p03.txt").read_text(), Path("briefs/p01.txt").read_text()
        self.assertIn("同一种字形", content)
        self.assertIn("字少不放大，字多不缩小", content)
        self.assertNotIn("字号锁", content)
        self.assertNotIn("标题的样子", cover)  # showcase pages compose their own lettering
        self.assertNotIn("text_locks", json.loads(Path("manifest-pages.json").read_text())[0])
        self.assertNotIn("标题的样子", "".join(delivery.expected_copy({"prompt_file": "briefs/p03.txt"})))

    def test_free_layout_with_every_title_in_one_spot_is_called_out(self):
        self.choose("free")
        plan = "## 全篇常量块\n画布为PPT。\n" + "".join(
            self.sourced(n, f"标题{n}", anchor=None if n == 1 else 1, lock="") for n in range(1, 5))
        code, out, _ = self.extraction(plan)
        self.assertEqual(code, 0)
        self.assertTrue(any("和统一版式没有区别" in warning for warning in json.loads(out)["warnings"]))

    def test_decks_without_the_choice_are_split_as_before(self):
        code, out, _ = self.extraction("## 全篇常量块\n画布为PPT。\n" + self.sourced(1, "标题", lock=""))
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out)["title_layout"])
        self.assertNotIn("标题的样子", Path("briefs/p01.txt").read_text())

    def test_over_budget_title_fails_before_any_output_changes(self):
        Path("manifest-anchors.json").write_text("unchanged")
        code, _, err = self.extraction(self.constants + self.page(1, "这个标题长到在锁定字号下一行根本放不下去也不允许缩小"))
        self.assertEqual(code, 1)
        self.assertIn("主标题 + 副标题", err)
        self.assertEqual(Path("manifest-anchors.json").read_text(), "unchanged")

    def test_unused_group_and_wider_canvas_are_reported_and_applied(self):
        self.workflow["brief"]["aspect_ratio"] = "21:9"
        code, out, _ = self.extraction(self.constants + SUB + "\n" + self.page(1, "四字标题"))
        self.assertEqual(code, 0)
        self.assertTrue(any("章节小标题 is declared but no copy line" in w for w in json.loads(out)["warnings"]))
        lock = json.loads(Path("manifest-anchors.json").read_text())[0]["text_locks"][0]
        self.assertAlmostEqual(lock["char_width_pct"], 6.5 / (21 / 9) * 1.05, places=2)

    def test_feedback_reaches_only_the_drifted_page_brief(self):
        content = self.constants + self.page(1, "四字标题") + self.page(2, "第二标题", anchor=1)
        self.assertEqual(self.extraction(content)[0], 0)
        before = {name: Path(name).read_bytes() for name in ("briefs/p01.txt", "briefs/p02.txt")}
        Path("text-lock-feedback.json").write_text(json.dumps({"schema_version": 1, "pages": {"2": {
            "image_sha256": "x", "attempt": 1, "items": [{"text": "第二标题", "group": "标题", "note": "“第二标题”上次字画大了。"}]}}},
            ensure_ascii=False))
        self.assertEqual(self.extraction(content)[0], 0)
        self.assertEqual(Path("briefs/p01.txt").read_bytes(), before["briefs/p01.txt"])
        self.assertIn("上一次生成的偏差", Path("briefs/p02.txt").read_text())
        self.assertIn("上次字画大了", Path("briefs/p02.txt").read_text())
        Path("text-lock-feedback.json").write_text("[]")
        self.assertEqual(self.extraction(content)[0], 1)  # a broken feedback file is never ignored silently


class MeasurementTests(unittest.TestCase):
    def test_line_drawn_in_its_box_is_ok(self):
        lock, image = make_lock(TITLE, "冲煮者的观念转变"), slide()
        draw_line(image, lock)
        item = text_lock.analyze(image, [lock])["items"][0]
        self.assertEqual(item["status"], "ok")
        self.assertAlmostEqual(item["measured"]["ink_height_pct"], 6.5, delta=0.15)

    def test_enlarged_and_shifted_lines_are_drift_with_signed_numbers(self):
        lock = make_lock(TITLE, "冲煮的基本逻辑")
        bigger, moved = slide(), slide()
        draw_line(bigger, lock, scale=1.15)
        draw_line(moved, lock, dy=40)
        item = text_lock.analyze(bigger, [lock])["items"][0]
        self.assertEqual(item["status"], "drift")
        self.assertAlmostEqual(item["deviation"]["height"], 0.15, delta=0.02)
        item = text_lock.analyze(moved, [lock])["items"][0]
        self.assertEqual(item["status"], "drift")
        self.assertAlmostEqual(item["deviation"]["y_pct"], 40 / 864 * 100, delta=0.2)
        self.assertEqual(text_lock.analyze(bigger, [lock], tolerance_height=0.2)["items"][0]["status"], "ok")
        nudged = slide()
        draw_line(nudged, lock, dx=-15, dy=-20)  # the offset seen with the box instruction alone
        self.assertEqual(text_lock.analyze(nudged, [lock])["items"][0]["status"], "ok")
        self.assertEqual(text_lock.analyze(nudged, [lock], tolerance_position=1.0)["items"][0]["status"], "drift")

    def test_stacked_lines_are_matched_to_their_own_locks(self):
        sub, main, image = make_lock(SUB, "第一步骤"), make_lock(MAIN, "冲煮方案"), slide()
        draw_line(image, sub)
        draw_line(image, main, scale=0.9)
        items = text_lock.analyze(image, [main, sub])["items"]
        self.assertEqual([(item["text"], item["status"]) for item in items], [("冲煮方案", "drift"), ("第一步骤", "ok")])
        self.assertAlmostEqual(items[0]["deviation"]["height"], -0.1, delta=0.02)

    def test_artwork_crossing_the_window_and_stray_specks_do_not_disturb_a_line(self):
        lock, image = make_lock(TITLE, "设计冲煮方案四要素"), slide()
        draw_line(image, lock)
        draw = ImageDraw.Draw(image)
        draw.rectangle([900, 0, 1535, 300], fill=(95, 168, 58))      # an island leaving the canvas
        draw.rectangle([760, 150, 790, 175], fill=(217, 69, 47))     # a small flower below the line
        for x, y in ((300, 40), (520, 125), (64, 100)):              # generation noise
            draw.point((x, y), fill=INK)
        self.assertEqual(text_lock.analyze(image, [lock])["items"][0]["status"], "ok")

    def test_busy_background_and_missing_lines_are_unlocated_not_guessed(self):
        lock = make_lock(TITLE, "冲煮方案设计表")
        self.assertEqual(text_lock.analyze(slide(), [lock])["items"][0],
                         {"text": "冲煮方案设计表", "group": "标题", "status": "unlocated", "reason": "line_not_isolated",
                          "expected": {"ink_height_pct": 6.5, "top_pct": 7.0, "left_pct": 4.6}})
        striped = slide()
        draw = ImageDraw.Draw(striped)
        for index, x in enumerate(range(0, CANVAS[0], 6)):
            draw.rectangle([x, 0, x + 5, CANVAS[1]], fill=[(20, 20, 20), (240, 120, 40), (60, 90, 200)][index % 3])
        draw_line(striped, lock)
        self.assertEqual(text_lock.analyze(striped, [lock])["items"][0]["reason"], "busy_background")

    def test_jobs_without_a_stored_box_are_measured_from_the_spec(self):
        lock, image = make_lock(TITLE, "辅导是最好的激励"), slide()
        draw_line(image, lock)
        legacy = {key: value for key, value in lock.items() if key not in {"box_pct", "units", "char_width_pct"}}
        self.assertEqual(text_lock.analyze(image, [legacy])["items"][0]["status"], "ok")


class FeedbackAndGateTests(InDeck):
    def job(self, page, text, scale=1.0, lock_line=TITLE):
        lock, image = make_lock(lock_line, text), slide()
        draw_line(image, lock, scale=scale)
        path = Path(f"images/p{page:02d}.png")
        path.parent.mkdir(exist_ok=True)
        image.save(path)
        return {"page": page, "output": str(path), "prompt": f"可见文字={text}；位置=左上；锁=标题", "text_locks": [lock]}

    def write(self, jobs, name="manifest.json"):
        Path(name).write_text(json.dumps(jobs, ensure_ascii=False))
        return name

    def run_check(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            return text_lock.main(["check", *argv]), out.getvalue()

    def test_check_exit_code_group_spread_and_feedback_lifecycle(self):
        manifest = self.write([self.job(1, "冲煮者的观念转变"), self.job(2, "冲煮的基本逻辑", scale=1.2)])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):  # redraws are the user's pick
            self.run_check("--manifest", manifest, "--feedback", "text-lock-feedback.json")
        self.assertFalse(Path("text-lock-feedback.json").exists())
        code, out = self.run_check("--manifest", manifest, "--feedback", "text-lock-feedback.json", "--pages", "1",
                                   "--report", "report.json")
        self.assertEqual(json.loads(out)["feedback"]["pages_with_new_feedback"], [])  # P2 drifted but was not chosen
        code, out = self.run_check("--manifest", manifest, "--feedback", "text-lock-feedback.json", "--pages", "2",
                                   "--report", "report.json")
        self.assertEqual(code, 1)
        summary = json.loads(out)
        self.assertEqual([page["status"] for page in summary["pages"]], ["ok", "drift"])
        self.assertAlmostEqual(summary["groups"]["标题"]["spread"], 0.2, delta=0.03)
        self.assertEqual(summary["feedback"]["pages_with_new_feedback"], [2])
        note = text_lock.load_feedback("text-lock-feedback.json")[2][0]["note"]
        self.assertRegex(note, r"“冲煮的基本逻辑”上次字画大了：字高是画布高度的 7\.\d%，应为 6\.5%，大了 \d\d%。这次以规定的墨迹字高和定位锚点为准")
        self.assertIn("保留字形比例与自然行宽", note)
        self.assertNotIn("填满", note)
        stored = Path("text-lock-feedback.json").read_bytes()
        # Checking the same image again records nothing new.
        self.assertEqual(json.loads(self.run_check("--manifest", manifest, "--feedback", "text-lock-feedback.json", "--pages", "all")[1])
                         ["feedback"]["pages_with_new_feedback"], [])
        self.assertEqual(Path("text-lock-feedback.json").read_bytes(), stored)
        # A redraw that lands inside the lock keeps its note, so the accepted brief never changes.
        manifest = self.write([self.job(1, "冲煮者的观念转变"), self.job(2, "冲煮的基本逻辑")])
        code, out = self.run_check("--manifest", manifest, "--feedback", "text-lock-feedback.json", "--pages", "2")
        self.assertEqual(code, 0)
        self.assertEqual(Path("text-lock-feedback.json").read_bytes(), stored)
        # A redraw that drifts again replaces the note and counts the attempt.
        manifest = self.write([self.job(1, "冲煮者的观念转变"), self.job(2, "冲煮的基本逻辑", scale=0.85)])
        self.assertEqual(self.run_check("--manifest", manifest, "--feedback", "text-lock-feedback.json", "--pages", "2")[0], 1)
        entry = json.loads(Path("text-lock-feedback.json").read_text())["pages"]["2"]
        self.assertEqual(entry["attempt"], 2)
        self.assertIn("字画小了", entry["items"][0]["note"])

    def test_missing_image_is_reported_without_a_crash(self):
        job = self.job(1, "冲煮者的观念转变")
        Path(job["output"]).unlink()
        code, out = self.run_check("--manifest", self.write([job]))
        self.assertEqual((code, json.loads(out)["pages"][0]["status"]), (1, "missing"))

    def notes(self, job, **extra):
        return {str(job["page"]): {"status": "passed", "reviewer": "fixture reviewer", "reviewed_at": "2026-09-09T10:00:00+08:00",
                                   "image_sha256": hashlib.sha256(Path(job["output"]).read_bytes()).hexdigest(),
                                   "checks": ["text", "layout", "style"], **extra}}

    def test_delivery_blocks_drift_until_redrawn_or_accepted_on_record(self):
        good = self.job(1, "冲煮者的观念转变")
        report = delivery.inspect_jobs([good], "16:9", "none", self.notes(good))
        self.assertTrue(report["review_complete"])
        self.assertEqual(report["pages"][0]["text_lock"]["items"][0]["status"], "ok")
        drifted = self.job(1, "冲煮的基本逻辑", scale=1.2)  # the same page, redrawn too large
        report = delivery.inspect_jobs([drifted], "16:9", "none", self.notes(drifted))
        self.assertFalse(report["structural_ok"])
        self.assertEqual(report["pages"][0]["errors"], ["text_lock_drift"])
        # A looser tolerance is an explicit choice, not a silent pass.
        loose = delivery.inspect_jobs([drifted], "16:9", "none", self.notes(drifted), lock_tolerances={"tolerance_height": 0.25})
        self.assertTrue(loose["structural_ok"])
        accepted = self.notes(drifted, text_lock_accepted={"by": "user", "note": "差一点可以接受"})
        report = delivery.inspect_jobs([drifted], "16:9", "none", accepted)
        self.assertTrue(report["review_complete"])
        self.assertEqual(report["pages"][0]["warnings"][-1], "text_lock_drift_accepted")
        # Acceptance is bound to the reviewed image and needs both who and why.
        stale = self.notes(drifted, text_lock_accepted={"by": "user", "note": "ok"})
        stale["1"]["image_sha256"] = "0" * 64
        self.assertFalse(delivery.inspect_jobs([drifted], "16:9", "none", stale)["structural_ok"])
        empty = self.notes(drifted, text_lock_accepted={"by": "user", "note": " "})
        self.assertFalse(delivery.inspect_jobs([drifted], "16:9", "none", empty)["structural_ok"])

    def test_unlocated_line_needs_an_explicit_visual_check(self):
        job = self.job(1, "冲煮者的观念转变")
        Image.new("RGB", CANVAS, MINT).save(job["output"])  # the line is not there
        report = delivery.inspect_jobs([job], "16:9", "none", self.notes(job))
        self.assertTrue(report["structural_ok"])
        self.assertFalse(report["review_complete"])
        self.assertIn("text_lock", report["pages"][0]["visual_review"]["required_checks"])
        notes = self.notes(job)
        notes["1"]["checks"].append("text_lock")
        self.assertTrue(delivery.inspect_jobs([job], "16:9", "none", notes)["review_complete"])

    def test_cli_refuses_to_assemble_drift_and_assembles_after_acceptance(self):
        job = self.job(1, "冲煮的基本逻辑", scale=1.2)
        manifest = self.write([job])
        with contextlib.redirect_stdout(io.StringIO()):
            code = delivery.main(["--manifest", manifest, "--ocr", "none", "--report", "quality.json", "--output-pptx", "deck.pptx"])
        self.assertEqual(code, 1)
        self.assertFalse(Path("deck.pptx").exists())
        Path("notes.json").write_text(json.dumps(self.notes(job, text_lock_accepted={"by": "user", "note": "这页就这样"}), ensure_ascii=False))
        with contextlib.redirect_stdout(io.StringIO()):
            code = delivery.main(["--manifest", manifest, "--ocr", "none", "--report", "quality.json",
                                  "--review-notes", "notes.json", "--output-pptx", "deck.pptx"])
        self.assertEqual(code, 0)
        self.assertTrue(Path("deck.pptx").exists())


class LengthRuleTests(unittest.TestCase):
    """字数统一: the model draws short lines large and long ones small."""

    def usage(self, line, *texts):
        groups = text_lock.parse_lock_groups(line)
        name = next(iter(groups))
        locks = {number: [{"group": name, "text": text, "units": text_lock.text_units(text)}]
                 for number, text in enumerate(texts, 1)}
        return text_lock.length_problems(locks, groups)

    def test_short_display_lines_share_one_exact_count(self):
        self.assertEqual(self.usage(MAIN, "冲煮方案", "注水手法", "稳定出品"), [])
        problems = self.usage(MAIN, "冲煮方案", "练习注水手法")
        self.assertEqual(len(problems), 1)
        self.assertIn("每页字数相同", problems[0])
        self.assertIn("4 字：P1；6 字：P2", problems[0])

    def test_longer_titles_differ_by_at_most_two(self):
        self.assertEqual(self.usage(TITLE, "冲煮者的观念转变", "冲煮的基本逻辑", "区分风味缺陷与参数"), [])
        problems = self.usage(TITLE, "课程目标", "冲煮者的观念转变", "边冲边学：稳定出品的基本原理")
        self.assertIn("最多差 2 个字", problems[0])
        self.assertIn("主标题 + 副标题", problems[0])

    def test_a_stated_count_replaces_the_default(self):
        self.assertEqual(self.usage(TITLE + "，字数 4–14", "课程目标", "边冲边学：稳定出品的基本原理"), [])
        problems = self.usage(MAIN + "，字数 4", "冲煮方案", "注水手法练习")
        self.assertIn("写明字数 4", problems[0])
        self.assertIn("P2“注水手法练习”6 个字", problems[0])
        for bad in ("，字数 0", "，字数 9–7", "，字数 4，字数 5", "，样板"):
            with self.assertRaises(ValueError):
                text_lock.parse_lock_groups(TITLE + bad)

    def test_planning_fields_stay_out_of_the_prompt(self):
        constants = "画布为PPT。\n" + TITLE + "，字数 7–9，样板 assets/typography/size-title.png\n- 保真：只画清单里的字。\n"
        self.assertEqual(text_lock.prompt_constants(constants), "画布为PPT。\n" + TITLE + "\n- 保真：只画清单里的字。\n")
        self.assertEqual(text_lock.parse_lock_groups(constants)["标题"]["template"], "assets/typography/size-title.png")


class TemplateTests(InDeck):
    def page_image(self, page, text, scale=1.0, busy=False):
        lock, image = make_lock(TITLE, text), slide()
        if busy:
            draw = ImageDraw.Draw(image)
            for index, x in enumerate(range(0, CANVAS[0], 6)):
                draw.rectangle([x, 0, x + 5, CANVAS[1]], fill=[(20, 20, 20), (240, 120, 40), (60, 90, 200)][index % 3])
        ImageDraw.Draw(image).rectangle([900, 500, 1200, 800], fill=(200, 80, 60))  # artwork the template must drop
        draw_line(image, lock, scale=scale)
        right = text_lock.analyze(image, [lock])["items"][0]["box"][2] if not busy else 0
        # Tall artwork 8 px to the right of the line, inside the template margin; measurement ignores it
        # because it runs out of the search window.
        ImageDraw.Draw(image).rectangle([right + 8, 0, right + 60, 400], fill=(90, 160, 70))
        path = Path(f"images/p{page:02d}.png")
        path.parent.mkdir(exist_ok=True)
        image.save(path)
        return {"page": page, "output": str(path), "text_locks": [lock], "art_x": right + 12}

    def test_template_keeps_only_the_chosen_line_and_the_lock_follows_the_chosen_page(self):
        jobs = [self.page_image(11, "设计冲煮方案四要素", scale=0.98), self.page_image(12, "目标分析与任务分析", scale=1.2)]
        result = text_lock.template(jobs, 11, ["标题"], "assets/typography/size-title.png")
        self.assertEqual(result["lock_updates"], [])
        with Image.open(result["template"]) as made:
            self.assertEqual(made.size, CANVAS)
            self.assertEqual(made.getpixel((1000, 600)), MINT)  # the artwork is gone
            lock = jobs[0]["text_locks"][0]
            self.assertEqual(made.getpixel((jobs[0]["art_x"], 80)), MINT)  # even right next to the line
            x = round(lock["box_pct"][0] * CANVAS[0] / 100) + 5
            y = round((lock["box_pct"][1] + lock["box_pct"][3]) / 2 * CANVAS[1] / 100)
            self.assertEqual(made.getpixel((x, y)), INK)  # the line itself is kept
        self.assertIn("样板 assets/typography/size-title.png", result["next"])
        # The user prefers the larger title on P12: the template follows it and the lock is updated to match.
        result = text_lock.template(jobs, 12, ["标题"], "assets/typography/size-title-b.png")
        self.assertAlmostEqual(result["lock_updates"][0]["ink_height_pct"], 7.8, delta=0.2)
        self.assertIn("以这一页为准", result["next"])

    def test_template_can_set_the_approved_line_at_another_groups_position(self):
        right = make_lock("- 字号锁 标题右：字高 6.5%，顶 7%，右 95%，最宽 58%", "七个字的右上标题")
        middle = make_lock("- 字号锁 标题中：字高 8%，顶 10%，居中 50%，最宽 80%", "居中位置的九字标题")  # same length as the cut line
        jobs = [self.page_image(11, "设计冲煮方案四要素", scale=1.2),
                {"page": 12, "output": "images/p12.png", "text_locks": [right]},
                {"page": 13, "output": "images/p13.png", "text_locks": [middle]}]
        result = text_lock.template(jobs, 11, ["标题"], "size-right.png", as_group="标题右")
        self.assertEqual(result["lock_updates"], [])  # scaled to the other group's height: no lock changes
        self.assertIn("字号锁 标题右 这一行末尾加“，样板 size-right.png”", result["next"])
        with Image.open("size-right.png") as made:
            self.assertEqual(made.size, CANVAS)
            self.assertEqual(made.getpixel((1000, 600)), MINT)  # artwork dropped
            found = text_lock.analyze(made.convert("RGB"), [right])["items"][0]
        self.assertEqual(found["status"], "ok")  # the 1.2x line now has the right group's height and right edge
        self.assertAlmostEqual(found["measured"]["ink_height_pct"], 6.5, delta=0.2)
        self.assertAlmostEqual(found["measured"]["right_pct"], 95, delta=0.5)
        text_lock.template(jobs, 11, ["标题"], "size-middle.png", as_group="标题中")
        with Image.open("size-middle.png") as made:
            found = text_lock.analyze(made.convert("RGB"), [middle])["items"][0]
        self.assertAlmostEqual(found["measured"]["ink_height_pct"], 8, delta=0.25)
        self.assertAlmostEqual(found["measured"]["center_x_pct"], 50, delta=0.5)
        with self.assertRaisesRegex(ValueError, "No page in the manifests uses 字号锁 标题底"):
            text_lock.template(jobs, 11, ["标题"], "size-bottom.png", as_group="标题底")

    def test_template_refuses_unreadable_pages_and_silent_overwrites(self):
        jobs = [self.page_image(1, "设计冲煮方案四要素"), self.page_image(2, "目标分析与任务分析", busy=True)]
        with self.assertRaisesRegex(ValueError, "flat background"):
            text_lock.template(jobs, 2, ["标题"], "size.png")
        with self.assertRaisesRegex(ValueError, "no line locked"):
            text_lock.template(jobs, 1, ["章节主标题"], "size.png")
        with self.assertRaisesRegex(ValueError, "must not overwrite"):
            text_lock.template(jobs, 1, ["标题"], jobs[0]["output"])
        text_lock.template(jobs, 1, ["标题"], "size.png")
        with self.assertRaisesRegex(ValueError, "--overwrite"):
            text_lock.template(jobs, 1, ["标题"], "size.png")
        text_lock.template(jobs, 1, ["标题"], "size.png", overwrite=True)

    def test_template_cli_keeps_existing_manifest_syntax(self):
        jobs = [self.page_image(1, "设计冲煮方案四要素")]
        Path("manifest.json").write_text(json.dumps(jobs), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = text_lock.main(["template", "--manifest", "manifest.json", "--page", "1",
                                   "--group", "标题", "--out", "size.png"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["template"], "size.png")
        self.assertTrue(Path("size.png").is_file())

    def test_every_page_of_the_group_gets_the_template_as_a_size_reference(self):
        Path("assets/typography").mkdir(parents=True)
        slide().save("assets/typography/size-title.png")
        constants = self.constants.replace("最宽 58%", "最宽 58%，样板 assets/typography/size-title.png")
        code, out, err = self.extraction(constants + self.page(1, "四字标题") + self.page(2, "第二标题", anchor=1)
                                         + self.page(3, "无锁页面", anchor=1, lock=""))
        self.assertEqual(code, 0, err)
        jobs = {job["page"]: job for name in ("manifest-anchors.json", "manifest-pages.json")
                for job in json.loads(Path(name).read_text())}
        for page in (1, 2):
            sizes = [ref for ref in jobs[page]["references"] if ref["role"] == "size"]
            self.assertEqual([(ref["id"], ref["path"]) for ref in sizes], [("SZ1", "assets/typography/size-title.png")])
            self.assertIn("字号样板 [SZ1]", Path(f"briefs/p0{page}.txt").read_text())
        self.assertFalse(any(ref["role"] == "size" for ref in jobs[3]["references"]))
        self.assertNotIn("样板", Path("deck-constants.txt").read_text())
        self.assertNotIn("还没有字号样板", out)
        binding = deck_runtime.reference_instructions(jobs[2]["references"])
        self.assertIn("[SZ1] (size)", binding)
        self.assertRegex(binding, r"字号|几何|定位")
        Path("assets/typography/size-title.png").unlink()
        self.assertEqual(self.extraction(constants + self.page(1, "四字标题"))[0], 1)  # a named template must exist

    def test_uneven_titles_fail_the_split_before_outputs_change(self):
        Path("manifest-anchors.json").write_text("unchanged")
        code, _, err = self.extraction(self.constants + self.page(1, "课程目标") + self.page(2, "冲煮者的观念转变", anchor=1))
        self.assertEqual(code, 1)
        self.assertIn("字数不统一", err)
        self.assertEqual(Path("manifest-anchors.json").read_text(), "unchanged")


class GuideTests(unittest.TestCase):
    """Native geometry guides: rendered ink, verification and output protection."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppt-native-guide-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fonts = ["/System/Library/Fonts/STHeiti Medium.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
                 "C:/Windows/Fonts/arial.ttf"]
        try:
            fonts.append(str(ImageFont.truetype("DejaVuSans.ttf", 16).path))
        except OSError:
            pass
        self.font = next((Path(path) for path in fonts if Path(path).is_file()), None)
        if self.font is None:
            self.skipTest("a real native font file is required")
        self.text = "四类服务参考报价" if "STHeiti" in self.font.name else "AgjpQWxy"

    def make(self, out="guide.png", **changes):
        options = dict(canvas="1672x941", text=self.text, font=self.font,
                       ink_height=6.2, left=5.5, top=5.5, out=self.root / out,
                       background="#8FE1FC", color="#000000")
        options.update(changes)
        return text_lock.guide(**options)

    def test_guide_calibrates_ink_and_contains_only_the_single_line(self):
        result = self.make()
        self.assertEqual(result["purpose"], "geometry-only")
        self.assertEqual(result["canvas"], [1672, 941])
        self.assertEqual(result["text"], self.text)
        self.assertEqual(result["font"], str(self.font.resolve()))
        self.assertNotEqual(result["font_size"], round(941 * 0.062))
        self.assertLessEqual(abs(result["ink_height_pct"] - 6.2), 100 / 941)
        self.assertLessEqual(abs(result["left_pct"] - 5.5), 50 / 1672)
        self.assertLessEqual(abs(result["top_pct"] - 5.5), 50 / 941)
        self.assertEqual(result["measurement"]["items"][0]["status"], "ok")
        self.assertEqual(result["measurement"]["items"][0]["box"], result["ink_box"])
        self.assertEqual(json.loads((self.root / "guide.json").read_text()), result)
        with Image.open(self.root / "guide.png") as image:
            self.assertEqual((image.format, image.size), ("PNG", (1672, 941)))
            difference = ImageChops.difference(image.convert("RGB"), Image.new("RGB", image.size, (143, 225, 252)))
            self.assertEqual(list(difference.getbbox()), result["ink_box"])
            # No marks, labels, scene artwork or other ink exists outside the one line.
            x0, y0, x1, y1 = result["ink_box"]
            for box in ((0, 0, 1672, y0), (0, y1, 1672, 941), (0, y0, x0, y1), (x1, y0, 1672, y1)):
                self.assertIsNone(difference.crop(box).getbbox())
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["guide.json", "guide.png"])

    def test_guide_cli_needs_no_manifest_and_writes_sidecar(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = text_lock.main(["guide", "--canvas", "1672x941", "--text", self.text,
                                   "--font", str(self.font), "--ink-height", "6.2", "--left", "5.5",
                                   "--top", "5.5", "--background", "#8FE1FC", "--color", "#000000",
                                   "--out", str(self.root / "cli.png")])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(Path(result["metadata"]), self.root / "cli.json")
        self.assertEqual(result["measurement"]["items"][0]["status"], "ok")

    def test_invalid_input_never_leaves_an_output_pair(self):
        bad = [{"canvas": "0x941"}, {"canvas": "1672:941"}, {"text": "a\nb"}, {"text": "  "},
               {"font": self.root / "missing.ttf"}, {"ink_height": float("nan")},
               {"ink_height": 0.01}, {"top": 99}, {"color": "not-a-color"},
               {"color": "#8FE1FC"}, {"canvas": "320x180", "ink_height": 40}]
        for index, options in enumerate(bad):
            name = f"invalid-{index}.png"
            with self.subTest(options=options), self.assertRaises((ValueError, OSError)):
                self.make(name, **options)
            self.assertFalse((self.root / name).exists())
            self.assertFalse((self.root / name).with_suffix(".json").exists())
        self.assertFalse(list(self.root.iterdir()))

    def test_existing_png_or_metadata_requires_explicit_overwrite(self):
        first = self.make()
        before = {name: (self.root / name).read_bytes() for name in ("guide.png", "guide.json")}
        with self.assertRaisesRegex(ValueError, "--overwrite"):
            self.make(text=self.text[::-1])
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})
        second = self.make(text=self.text[::-1], overwrite=True)
        self.assertNotEqual(first["image_sha256"], second["image_sha256"])
        (self.root / "orphan.json").write_text("keep")
        with self.assertRaisesRegex(ValueError, "--overwrite"):
            self.make("orphan.png")
        self.assertFalse((self.root / "orphan.png").exists())
        self.assertEqual((self.root / "orphan.json").read_text(), "keep")

    def test_outputs_cannot_alias_input_font(self):
        for name, output in (("font.png", "font.png"), ("font.json", "font.png")):
            font = self.root / name
            font.write_bytes(self.font.read_bytes())
            original = font.read_bytes()
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "input font"):
                self.make(output, font=font, overwrite=True)
            self.assertEqual(font.read_bytes(), original)
            font.unlink()

    def test_failed_measurement_is_not_hidden_by_looser_tolerances(self):
        with mock.patch.object(text_lock, "measure", return_value={"items": [{"status": "drift"}]}) as measured:
            with self.assertRaisesRegex(ValueError, "calibration failed"):
                self.make()
        self.assertEqual(measured.call_args.kwargs["tolerance_height"], text_lock.TOLERANCE_HEIGHT)
        self.assertLessEqual(measured.call_args.kwargs["tolerance_position"], text_lock.TOLERANCE_POSITION)
        self.assertFalse(list(self.root.iterdir()))

    def test_failed_second_publish_removes_first_complete_file(self):
        real_link = text_lock.os.link

        def fail_metadata(source, destination):
            if Path(destination).suffix == ".json":
                raise OSError("fixture metadata publication failed")
            return real_link(source, destination)

        with mock.patch.object(text_lock.os, "link", side_effect=fail_metadata):
            with self.assertRaisesRegex(OSError, "publication failed"):
                self.make()
        self.assertFalse(list(self.root.iterdir()))

    def test_failed_overwrite_preserves_both_existing_outputs(self):
        self.make()
        original = {name: (self.root / name).read_bytes() for name in ("guide.png", "guide.json")}
        real_replace = text_lock.os.replace

        def fail_metadata(source, destination):
            if Path(source).name == "guide.json":
                raise OSError("fixture metadata replacement failed")
            return real_replace(source, destination)

        with mock.patch.object(text_lock.os, "replace", side_effect=fail_metadata):
            with self.assertRaisesRegex(OSError, "replacement failed"):
                self.make(text=self.text[::-1], overwrite=True)
        self.assertEqual(original, {name: (self.root / name).read_bytes() for name in original})
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["guide.json", "guide.png"])


if __name__ == "__main__":
    unittest.main()
