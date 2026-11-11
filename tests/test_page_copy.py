"""Copy-sheet regressions: every on-screen line is listed and marked in one document before
any design, and a plan may show nothing else. Fictional material; no network."""
import contextlib
import hashlib
import io
import json
import unittest
from pathlib import Path

import test_content_map as base  # the same fictional source, skeleton and aligned nine-page plan
import extract_briefs as extract
import page_copy as pc


def sheet(pages=base.ALIGNED, marks=None, extra=None):
    """A copy sheet for `pages`; marks: {text: mark}, extra: {page number: [raw lines]}."""
    marks, extra = marks or {}, extra or {}
    lines = ["# 文案稿（TEST ONLY）", ""]
    for number, (source, copy) in enumerate(pages, 1):
        lines += [f"## 第{number}页", f"- 内容来源：{source}"]
        lines += [f"- 上屏：{text}｜{marks.get(text, '压缩')}" for text in copy]
        lines += extra.get(number, [])
    return "\n".join(lines) + "\n"


class CopySheet(base.InFolder):
    def setUp(self):
        super().setUp()
        Path("content-map.md").write_text(base.SKELETON, encoding="utf-8")

    def check(self, text, page_count=9, skeleton=None):
        if skeleton is not None:
            Path("content-map.md").write_text(skeleton, encoding="utf-8")
        Path("page-copy.md").write_text(text, encoding="utf-8")
        return pc.check_copy("page-copy.md", "content-map.md", None, page_count)


class SheetTests(CopySheet):
    def test_valid_sheet_reports_pages_lines_marks_and_coverage(self):
        report = self.check(sheet(marks={"80%": "原文"}))
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual((report["pages"], report["lines"]), (9, 23))
        self.assertEqual(report["marks"], {"原文": 1, "压缩": 22, "新增": 0, "引语": 0})
        self.assertEqual((report["added"], report["quotes"]), ([], []))
        self.assertEqual(report["coverage"]["on_screen"], 5)
        self.assertEqual(pc.summary_line(report), "9 页 23 行上屏文字：原文 1、压缩 22、新增 0、引语 0")

    def test_every_line_needs_a_mark_and_every_page_a_source(self):
        cases = {
            "没有标出处": sheet().replace("- 上屏：课程名｜压缩", "- 上屏：课程名"),
            "上屏文字是空的": sheet().replace("- 上屏：课程名｜压缩", "- 上屏：｜压缩"),
            "不能有分号": sheet().replace("- 上屏：课程名｜压缩", "- 上屏：课程名；副题｜压缩"),
            "P1 没有写“- 内容来源": sheet().replace("- 内容来源：封面\n", ""),
            "P4 没有上屏文字": sheet().replace("- 上屏：豆子与器具｜压缩\n## 第5页", "## 第5页"),
            "第 9 页重复": sheet() + "## 第9页\n- 内容来源：结尾\n- 上屏：谢谢｜新增：TEST\n",
            "页码要从第 1 页连续排": sheet().replace("## 第9页", "## 第10页"),
        }
        for expected, text in cases.items():
            with self.subTest(expected=expected):
                report = self.check(text)
                self.assertFalse(report["ok"])
                self.assertTrue(any(expected in problem for problem in report["problems"]), report["problems"])
        self.assertTrue(any("没有页面" in problem for problem in self.check("# 只有标题\n")["problems"]))
        ascii_bar = self.check(sheet().replace("课程名｜压缩", "课程名 | 压缩"))  # a plain | separates as well
        self.assertTrue(ascii_bar["ok"], ascii_bar["problems"])

    def test_page_count_must_match_the_confirmed_brief(self):
        report = self.check(sheet(), page_count=8)
        self.assertTrue(any("文案稿有 9 页，已确认的页数是 8 页" in problem for problem in report["problems"]))
        self.assertTrue(self.check(sheet(), page_count=None)["ok"])

    def test_verbatim_marks_are_checked_against_the_source(self):
        report = self.check(sheet(marks={"多练习": "原文"}))
        self.assertTrue(any("P3“多练习”标的是原文，但原文里找不到这句" in problem for problem in report["problems"]))
        self.assertTrue(self.check(sheet(marks={"80%": "原文"}))["ok"])
        no_source = base.SKELETON.replace("- 原文：source.md", "- 原文：无").replace(
            "“冲煮方案有四个要素：粉量、水温、研磨、时间”", "自拟")
        self.assertFalse(any("找不到这句" in p for p in self.check(sheet(marks={"多练习": "原文"}), skeleton=no_source)["problems"]))

    def test_added_lines_need_a_reason_and_are_listed_for_the_user(self):
        bare_addition = self.check(sheet(extra={5: ["- 上屏：行动起来｜新增"]}))
        self.assertTrue(any("P5“行动起来”是新增的字，要写理由" in problem for problem in bare_addition["problems"]))
        report = self.check(sheet(extra={8: ["- 上屏：01｜新增：四要素的序号"]}))
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["added"], [{"page": 8, "text": "01", "reason": "四要素的序号"}])

    def test_quotations_stay_off_a_knowledge_deck_unless_the_user_asked(self):
        quote = "- 上屏：并不是有了器具才有好咖啡｜引语"
        report = self.check(sheet(extra={8: [quote]}))
        self.assertTrue(any("知识型的引语、金句和署名默认不上屏" in problem for problem in report["problems"]))
        kept = self.check(sheet(extra={8: [quote + "：用户说这句要留"]}))
        self.assertTrue(kept["ok"], kept["problems"])
        self.assertEqual(kept["quotes"], [{"page": 8, "text": "并不是有了器具才有好咖啡", "reason": "用户说这句要留"}])
        invented = self.check(sheet(extra={8: ["- 上屏：好咖啡全靠运气｜引语：用户说这句要留"]}))
        self.assertTrue(any("标的是引语，但原文里找不到这句" in problem for problem in invented["problems"]))
        speech = self.check(sheet(extra={8: [quote]}), skeleton=base.SKELETON.replace("- 类型：知识型", "- 类型：演讲型"))
        self.assertTrue(speech["ok"], speech["problems"])  # a speech deck may carry it; it is still listed
        self.assertEqual(len(speech["quotes"]), 1)

    def test_skeleton_alignment_applies_before_any_design(self):
        dropped = self.check(sheet([item for item in base.ALIGNED if item[0] != "B2"]), page_count=8)
        self.assertTrue(any("B2《责任》既没有上屏" in problem for problem in dropped["problems"]))
        thin = list(base.ALIGNED)
        thin[4] = ("B1", ("观念转变", "一句金句"))
        self.assertTrue(any("P5（B1）只有 2 处文字" in warning for warning in self.check(sheet(thin))["warnings"]))
        Path("content-map.md").write_text("# 空的\n", encoding="utf-8")
        self.assertTrue(any("内容骨架还没通过检查" in problem for problem in self.check(sheet())["problems"]))

    def test_uneven_content_titles_draw_a_warning_not_a_failure(self):
        uneven = list(base.ALIGNED)
        uneven[7] = ("C1", ("冲煮方案的四个基本要素", "粉量", "水温"))
        report = self.check(sheet(uneven))
        self.assertTrue(report["ok"], report["problems"])
        self.assertTrue(any("内容页标题" in warning and "2 个字到 11 个字" in warning for warning in report["warnings"]))
        even = list(base.ALIGNED)  # titles written to within two characters of each other
        for index, title in ((2, "为何要学"), (4, "观念转变"), (5, "明确责任"), (7, "四个要素")):
            even[index] = (even[index][0], (title,) + even[index][1][1:])
        self.assertFalse(any("内容页标题" in warning for warning in self.check(sheet(even))["warnings"]))


class PlanTests(CopySheet):
    def pages(self, text=None):
        pages, problems = pc.parse_copy(text or sheet())
        self.assertEqual(problems, [])
        return pages

    def test_plan_must_show_exactly_the_confirmed_lines(self):
        self.assertEqual(pc.compare_plan(self.pages(), base.plan()), [])
        slogan = list(base.ALIGNED)
        slogan[4] = ("B1", ("观念转变", "从…到…", "三组", "改变从现在开始"))
        self.assertEqual(pc.compare_plan(self.pages(), base.plan(slogan)), ["P5 方案里多了没确认过的字：“改变从现在开始”"])
        cut = list(base.ALIGNED)
        cut[4] = ("B1", ("观念转变", "从…到…"))
        self.assertEqual(pc.compare_plan(self.pages(), base.plan(cut)), ["P5 方案里少了已确认的字：“三组”"])
        moved = list(base.ALIGNED)
        moved[4] = ("B2", moved[4][1])
        self.assertTrue(any("P5 的 [内容来源] 是“B2”，文案稿里是“B1”" in item for item in pc.compare_plan(self.pages(), base.plan(moved))))

    def test_page_sets_must_match_unless_the_split_is_partial(self):
        shorter = base.plan(base.ALIGNED[:8])
        self.assertEqual(pc.compare_plan(self.pages(), shorter), ["P9 在文案稿里有，方案里没有"])
        self.assertEqual(pc.compare_plan(self.pages(), shorter, partial=True), [])
        self.assertEqual(pc.compare_plan(self.pages(sheet(base.ALIGNED[:8])), base.plan()), ["P9 在方案里有，文案稿里没有"])

    def test_cli_checks_sheet_and_plan(self):
        Path("page-copy.md").write_text(sheet(), encoding="utf-8")
        Path("plans.md").write_text(base.plan(), encoding="utf-8")
        Path("deck-state.json").write_text(json.dumps({"workflow": {"brief": {"page_count": 9}}}))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = pc.main(["check", "--copy", "page-copy.md", "--map", "content-map.md", "--state", "deck-state.json",
                            "--plans", "plans.md"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["plan_differences"], [])
        slogan = list(base.ALIGNED)
        slogan[4] = ("B1", ("观念转变", "从…到…", "三组", "改变从现在开始"))
        Path("plans.md").write_text(base.plan(slogan), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pc.main(["check", "--copy", "page-copy.md", "--map", "content-map.md", "--plans", "plans.md"]), 1)


class SplitTests(CopySheet):
    def setUp(self):
        super().setUp()
        Path("fixture-visual.md").write_text("TEST ONLY: visual and five typography roles shown.")
        Path("fixture-refs.md").write_text("TEST ONLY: reviewed reference shortlist.")
        roles = {name: {"familyIntent": "TEST ONLY: readable sans serif"} for name in ("display", "title", "body", "label", "data")}
        Path("fixture-style.json").write_text(json.dumps({"schemaVersion": 2, "typography": {"roles": roles}}))
        Path("page-copy.md").write_text(sheet(), encoding="utf-8")
        decision = {"status": "confirmed", "source": "user", "evidence": "TEST FIXTURE: confirm presented proposal",
                    "confirmed_at": "2026-09-09T10:00:00+08:00"}
        confirmed = hashlib.sha256(Path("page-copy.md").read_bytes()).hexdigest()
        self.workflow = {"schema_version": 1, "mode": "standard", "content_map_required": True, "page_copy_required": True,
            "brief": {"page_count": 9, "aspect_ratio": "16:9", "audience": "test audience", "purpose": "test presentation"},
            "decisions": {"intake": dict(decision), "visual": dict(decision),
                          "copy": {**decision, "artifacts_sha256": {"page_copy": confirmed}}},
            "artifacts": {"content_map": "content-map.md", "page_copy": "page-copy.md", "visual_proposal": "fixture-visual.md",
                          "inspiration_review": "fixture-refs.md", "style_config": "fixture-style.json"},
            "typography_decision": {"specimen": "skip", "reason": "TEST ONLY: ordinary type", "summary": "TEST ONLY: five readable text roles"},
            "pilot_pages": [1]}

    def split(self, content):
        Path("plans.md").write_text(content, encoding="utf-8")
        Path("deck-state.json").write_text(json.dumps({"source_files": ["source.md"], "workflow": self.workflow}))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = extract.main(["plans.md"])
        return code, out.getvalue(), err.getvalue()

    def test_split_passes_when_the_plan_shows_the_confirmed_copy(self):
        code, _, err = self.split(base.plan())
        self.assertEqual(code, 0, err)
        self.assertTrue(Path("briefs/p05.txt").is_file())

    def test_a_line_added_in_the_plan_stops_the_split_before_any_output(self):
        slogan = list(base.ALIGNED)
        slogan[4] = ("B1", ("观念转变", "从…到…", "三组", "改变从现在开始"))
        code, _, err = self.split(base.plan(slogan))
        self.assertEqual(code, 1)
        self.assertIn("和已确认的文案稿不一致", err)
        self.assertIn("P5 方案里多了没确认过的字：“改变从现在开始”", err)
        self.assertFalse(Path("briefs").exists())

    def test_sheet_changed_or_missing_after_confirmation_blocks_the_gate(self):
        Path("page-copy.md").write_text(sheet().replace("多练习", "多多练习"), encoding="utf-8")
        code, _, err = self.split(base.plan())
        self.assertEqual(code, 1)
        self.assertIn("decisions.copy.changed", err)
        self.workflow["artifacts"].pop("page_copy")
        code, _, err = self.split(base.plan())
        self.assertEqual(code, 1)
        self.assertIn("artifacts.page_copy", err)
        self.workflow["decisions"].pop("copy")
        self.assertIn("decisions.copy", self.split(base.plan())[2])


if __name__ == "__main__":
    unittest.main()
