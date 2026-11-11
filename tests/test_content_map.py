"""Content-skeleton regressions: the skeleton must quote the source verbatim, and every
page must answer for where its content comes from. Fictional material; no network."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("MOYU_PPT_CONFIG_DIR", tempfile.mkdtemp(prefix="ppt-config-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import content_map as cm
import extract_briefs as extract

SOURCE = """开场白
一杯好咖啡的味道
80%来自咖啡豆
新手入门之后的第一件事就是练习
稳定的注水手法。

豆子与器具
最难的转变是从个人口味到稳定出品。
冲煮的人是风味目标的第一负责人。
每个风味缺陷的背后，都对应着一个冲煮参数。

第一步：冲煮方案
冲煮方案有四个要素：粉量、水温、研磨、时间。
并不是有了器具才有好咖啡。
"""

SKELETON = """# 内容骨架（TEST ONLY）

- 类型：知识型
- 原文：source.md
- 主线：TEST 先认识豆子，再学冲煮方案。

## 逐节

### A 开场
#### A1 咖啡豆与 80%
- 讲者要讲什么：TEST 豆子很要紧。
- 靠什么讲清楚：TEST 一句引语和一张占比图。
- 原文依据：“一杯好咖啡的味道”“80%来自咖啡豆”
#### A2 新能力
- 讲者要讲什么：TEST 入门后先练注水。
- 靠什么讲清楚：TEST 一组对比。
- 原文依据：“新手入门之后的第一件事就是练习稳定的注水手法”

### B 豆子与器具
#### B1 观念转变
- 讲者要讲什么：TEST 从凭感觉变成看数据最难。
- 靠什么讲清楚：TEST 三组从…到…。
- 原文依据：“最难的转变是从个人口味到稳定出品”
#### B2 责任
- 讲者要讲什么：TEST 冲煮的人要负责。
- 靠什么讲清楚：TEST 三个概念。
- 原文依据：“冲煮的人是风味目标的第一负责人”“每个风味缺陷的背后，都对应着一个冲煮参数”
- 待确认：TEST 三个概念的先后是推断。

### C 第一步：冲煮方案
#### C1 四要素
- 讲者要讲什么：TEST 方案从四个方面定。
- 靠什么讲清楚：TEST 四步。
- 原文依据：“冲煮方案有四个要素：粉量、水温、研磨、时间”
#### C2 老师傅的话
- 讲者要讲什么：TEST 先有目标。
- 靠什么讲清楚：TEST 一句引语。
- 原文依据：“并不是有了器具才有好咖啡”
- 不上屏：TEST 讲师口述

## 页数怎么定
- 类型：这一行在节点之后，不是文件头
"""


def page(number, source, copy=("标题", "要点一", "要点二"), anchor=None):
    note = "本页为族锚定页" if anchor is None else f"锚定页 P{anchor}"
    field = f"**[内容来源]**：{source}\n" if source is not None else ""
    lines = "".join(f"可见文字={text}；位置=某处\n" for text in copy)
    return (f"## 第{number}页设计方案\n**[场景族]**：日光（{note}）\n{field}**[空间定位图]**\n构图与内容。\n"
            f"**可见文字清单**：\n{lines}")


ALIGNED = [("封面", ("课程名",)), ("目录", ("目录", "豆子与器具", "第一步", "冲煮方案")),
           ("A1+A2", ("为什么要学冲煮", "80%", "多练习")), ("章节页 B", ("豆子与器具",)), ("B1", ("观念转变", "从…到…", "三组")),
           ("B2", ("责任", "风味缺陷", "冲煮参数")), ("章节页 C", ("第一步", "冲煮方案")), ("C1", ("四要素", "粉量", "水温")),
           ("回顾 全篇", ("回顾", "豆子", "方案"))]


def plan(pages=ALIGNED):
    return "## 全篇常量块\n画布为PPT。\n" + "".join(
        page(number, source, copy, anchor=None if number == 1 else 1) for number, (source, copy) in enumerate(pages, 1))


class InFolder(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppt-content-map-")
        self.previous = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, self.previous)
        self.addCleanup(self.temp.cleanup)
        Path("source.md").write_text(SOURCE, encoding="utf-8")

    def check(self, text=SKELETON, state=None):
        Path("content-map.md").write_text(text, encoding="utf-8")
        return cm.check_map("content-map.md", state)

    def skeleton(self, text=SKELETON):
        report = self.check(text)
        self.assertTrue(report["ok"], report["problems"])
        return report["skeleton"]


class SkeletonTests(InFolder):
    def test_valid_skeleton_reports_type_counts_and_checked_quotes(self):
        report = self.check()
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual((report["type"], report["sections"], report["nodes"]), ("知识型", 3, 6))
        self.assertEqual((report["off_screen"], report["pending"]), (["C2"], ["B2"]))
        self.assertEqual(report["evidence"], {"quotes": 8, "not_found": [], "checked": True})

    def test_quotes_must_be_the_sources_own_words(self):
        report = self.check(SKELETON.replace("“冲煮的人是风味目标的第一负责人”", "“冲煮的人是这杯咖啡的灵魂”"))
        self.assertFalse(report["ok"])
        self.assertEqual(report["evidence"]["not_found"], [{"node": "B2", "quote": "冲煮的人是这杯咖啡的灵魂"}])
        self.assertTrue(any("B2 的依据在原文里找不到" in problem for problem in report["problems"]))
        missing = self.check(SKELETON.replace("- 原文依据：“最难的转变是从个人口味到稳定出品”\n", ""))
        self.assertTrue(any("B1 没有原文依据" in problem for problem in missing["problems"]))

    def test_matching_ignores_line_breaks_and_pdf_radical_forms(self):
        # The quote for A2 spans a hard line break in the source.
        self.assertTrue(self.check()["ok"])
        mangled = SOURCE.replace("个人口味", "个⼈口味").replace("目标的第一负责人", "⺫标的第一负责人")
        Path("source.md").write_text(mangled, encoding="utf-8")
        self.assertTrue(self.check()["ok"])
        # A radical-form character stands in for one character, not for arbitrary text.
        wrong = self.check(SKELETON.replace("风味目标的第一负责人", "风味指标的首要负责人"))
        self.assertFalse(wrong["ok"])

    def test_structure_rules_name_the_offending_item(self):
        cases = {
            "开头要有一行“- 类型": SKELETON.replace("- 类型：知识型\n", ""),
            "开头要有一行“- 类型：": SKELETON.replace("- 类型：知识型", "- 类型：汇报"),
            "主线": SKELETON.replace("- 主线：TEST 先认识豆子，再学冲煮方案。\n", ""),
            "B1 缺“讲者要讲什么”": SKELETON.replace("- 讲者要讲什么：TEST 从凭感觉变成看数据最难。\n", ""),
            "C1 缺“靠什么讲清楚”": SKELETON.replace("- 靠什么讲清楚：TEST 四步。\n", ""),
            "C2 标了不上屏但没写理由": SKELETON.replace("- 不上屏：TEST 讲师口述", "- 不上屏："),
            "B2 要写在“### B": SKELETON.replace("### B 豆子与器具\n", ""),
            "A1 重复": SKELETON.replace("#### A2 新能力", "#### A1 新能力"),
            "部分 B 重复": SKELETON.replace("### C 第一步：冲煮方案", "### B 第一步：冲煮方案"),
        }
        for expected, text in cases.items():
            with self.subTest(expected=expected):
                report = self.check(text)
                self.assertFalse(report["ok"])
                self.assertTrue(any(expected in problem for problem in report["problems"]), report["problems"])
        self.assertEqual(self.check(SKELETON.replace("- 类型：知识型", "- 类型：知识型（培训课件）"))["type"], "知识型")

    def test_sources_come_from_the_map_the_state_or_an_explicit_none(self):
        without_line = SKELETON.replace("- 原文：source.md\n", "")
        self.assertTrue(any("没有可核对的原文" in p for p in self.check(without_line)["problems"]))
        Path("deck-state.json").write_text(json.dumps({"source_files": ["source.md"]}), encoding="utf-8")
        self.assertTrue(self.check(without_line, "deck-state.json")["ok"])
        unreadable = self.check(SKELETON.replace("- 原文：source.md", "- 原文：missing.pdf"))
        self.assertTrue(any("missing.pdf 读不成文本" in p for p in unreadable["problems"]))
        none = self.check(SKELETON.replace("- 原文：source.md", "- 原文：无").replace("“冲煮方案有四个要素：粉量、水温、研磨、时间”", "自拟"))
        self.assertTrue(none["ok"], none["problems"])
        self.assertFalse(none["evidence"]["checked"])
        self.assertIn("没有原文", none["evidence"]["note"])


class AlignmentTests(InFolder):
    def problems(self, pages, text=SKELETON):
        result = cm.check_plan(self.skeleton(text), plan(pages))
        return result["problems"]

    def test_aligned_plan_passes_and_yields_the_coverage_table(self):
        skeleton = self.skeleton()
        result = cm.check_plan(skeleton, plan())
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["summary"], {"nodes": 6, "on_screen": 5, "merged": 2, "off_screen": 1,
                                             "pages": 9, "content_pages": 4})
        rows = {row["node"]: row for row in result["coverage"]}
        self.assertEqual((rows["A1"]["pages"], rows["A1"]["status"]), ([3], "上屏（与 A2 合并）"))
        self.assertEqual(rows["C2"]["status"], "不上屏：TEST 讲师口述")
        self.assertEqual(result["warnings"], ["B2 含有待确认的推断；上屏前请用户确认，不要当成原文的意思"])
        table = cm.coverage_table(skeleton, result)
        self.assertIn("原文共 6 节：上屏 5 节（其中 2 节与同级合并），不上屏 1 节。全篇 9 页，内容页 4 页。", table)
        self.assertIn("| C2 老师傅的话 | — | 不上屏：TEST 讲师口述 |", table)
        self.assertIn("## C 第一步：冲煮方案", table)

    def test_every_page_declares_a_readable_source(self):
        pages = list(ALIGNED)
        pages[5] = (None, ("责任", "风味缺陷", "冲煮参数"))
        self.assertTrue(any("P6 没有写 [内容来源]" in p for p in self.problems(pages)))
        for bad in ("第二章", "B2；移动", "B2 和 C1", "章节页"):
            pages[5] = (bad, ("责任", "风味缺陷", "冲煮参数"))
            with self.subTest(bad=bad):
                self.assertTrue(any("P6 的 [内容来源]" in p and "看不懂" in p for p in self.problems(pages)))
        pages[5] = ("B9", ("责任", "风味缺陷", "冲煮参数"))
        self.assertTrue(any("P6 指向的节点 B9 不在内容骨架里" in p for p in self.problems(pages)))

    def test_nothing_is_dropped_silently_and_off_screen_means_off_screen(self):
        pages = [item for item in ALIGNED if item[0] != "B2"]
        self.assertTrue(any("B2《责任》既没有上屏，也没有写“不上屏：理由”" in p for p in self.problems(pages)))
        pages = list(ALIGNED)
        pages[7] = ("C1+C2", ("四要素", "粉量", "水温"))
        self.assertTrue(any("C2 标了不上屏，却出现在 P8" in p for p in self.problems(pages)))

    def test_pages_stay_in_their_chapter_and_in_source_order(self):
        # The real failure: opening material placed behind the first chapter divider.
        misplaced = [ALIGNED[0], ALIGNED[1], ALIGNED[3], ALIGNED[2], *ALIGNED[4:]]
        self.assertTrue(any("部分 B《豆子与器具》的页不连续" in p for p in self.problems(misplaced)))
        cross = list(ALIGNED)
        cross[5] = ("B2+C1", ("责任", "风味缺陷", "冲煮参数"))
        self.assertTrue(any("P6 把不同部分的节点并在一页" in p for p in self.problems(cross)))
        late_divider = [*ALIGNED[:3], ALIGNED[4], ALIGNED[3], *ALIGNED[5:]]
        self.assertTrue(any("P5 是部分 B 的章节页，但 P4 已经先讲了这一部分" in p for p in self.problems(late_divider)))
        swapped = [*ALIGNED[:4], ALIGNED[5], ALIGNED[4], *ALIGNED[6:]]
        self.assertTrue(any("P6（B1）排在 P5 之后，和原文顺序相反" in p for p in self.problems(swapped)))
        swapped[5] = ("B1；移动：先讲责任更容易带出转变", ("观念转变", "从…到…", "三组"))
        self.assertEqual(self.problems(swapped), [])

    def test_toc_and_dividers_use_the_sources_chapter_titles(self):
        pages = list(ALIGNED)
        pages[6] = ("章节页 C", ("第一招", "做好规划"))
        self.assertTrue(any("P7 章节页上的字和这一部分的原题《第一步：冲煮方案》不一致" in p for p in self.problems(pages)))
        pages = list(ALIGNED)
        pages[1] = ("目录", ("目录", "豆子与器具", "规划"))
        self.assertTrue(any("P2 目录里找不到《第一步：冲煮方案》" in p for p in self.problems(pages)))

    def test_slogan_pages_are_flagged_for_knowledge_decks_only(self):
        pages = list(ALIGNED)
        pages[4] = ("B1", ("观念转变", "一句金句"))
        result = cm.check_plan(self.skeleton(), plan(pages))
        self.assertTrue(result["ok"])
        self.assertTrue(any("P5（B1）只有 2 处文字" in w for w in result["warnings"]))
        speech = cm.check_plan(self.skeleton(SKELETON.replace("- 类型：知识型", "- 类型：演讲型")), plan(pages))
        self.assertFalse(any("只有 2 处文字" in w for w in speech["warnings"]))


class WorkflowTests(InFolder):
    def setUp(self):
        super().setUp()
        Path("fixture-visual.md").write_text("TEST ONLY: visual and five typography roles shown.")
        Path("fixture-refs.md").write_text("TEST ONLY: reviewed reference shortlist.")
        roles = {name: {"familyIntent": "TEST ONLY: readable sans serif"} for name in ("display", "title", "body", "label", "data")}
        Path("fixture-style.json").write_text(json.dumps({"schemaVersion": 2, "typography": {"roles": roles}}))
        Path("content-map.md").write_text(SKELETON, encoding="utf-8")
        decision = {"status": "confirmed", "source": "user", "evidence": "TEST FIXTURE: confirm presented proposal",
                    "confirmed_at": "2026-09-09T10:00:00+08:00"}
        self.workflow = {"schema_version": 1, "mode": "standard", "content_map_required": True,
            "brief": {"page_count": 9, "aspect_ratio": "16:9", "audience": "test audience", "purpose": "test presentation"},
            "decisions": {"intake": dict(decision), "visual": dict(decision)},
            "artifacts": {"content_map": "content-map.md", "visual_proposal": "fixture-visual.md",
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

    def test_split_writes_the_coverage_table_for_an_aligned_plan(self):
        code, out, err = self.split(plan())
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["content"], {"type": "知识型", "nodes": 6, "on_screen": 5, "merged": 2, "off_screen": 1,
                                             "pages": 9, "content_pages": 4, "coverage_table": "coverage.md"})
        self.assertTrue(any("B2 含有待确认的推断" in warning for warning in report["warnings"]))
        self.assertIn("| B2 责任 | P6 | 上屏 |", Path("coverage.md").read_text(encoding="utf-8"))

    def test_misaligned_plan_or_broken_skeleton_stops_before_any_output(self):
        Path("manifest-pages.json").write_text("unchanged")
        code, _, err = self.split(plan([item for item in ALIGNED if item[0] != "B2"]).replace("第9页", "第9页"))
        self.assertEqual(code, 1)
        self.assertIn("B2《责任》既没有上屏", err)
        self.assertEqual(Path("manifest-pages.json").read_text(), "unchanged")
        self.assertFalse(Path("coverage.md").exists())
        Path("source.md").write_text("原文被换掉了。\n", encoding="utf-8")
        code, _, err = self.split(plan())
        self.assertEqual(code, 1)
        self.assertIn("artifacts.content_map", err)

    def test_new_decks_need_a_skeleton_and_older_states_split_as_before(self):
        self.workflow["artifacts"].pop("content_map")
        code, _, err = self.split(plan())
        self.assertEqual(code, 1)
        self.assertIn("artifacts.content_map", err)
        self.workflow.pop("content_map_required")  # a state without the flag
        legacy = "## 全篇常量块\n画布为PPT。\n" + "".join(
            page(number, None, anchor=None if number == 1 else 1) for number in range(1, 10))
        code, out, err = self.split(legacy)
        self.assertEqual(code, 0, err)
        self.assertIsNone(json.loads(out)["content"])
        self.assertFalse(Path("coverage.md").exists())

    def test_cli_checks_map_and_plan_and_writes_coverage(self):
        Path("plans.md").write_text(plan(), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cm.main(["check", "--map", "content-map.md", "--plans", "plans.md", "--coverage", "out/coverage.md"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())["plan"]["ok"])
        self.assertTrue(Path("out/coverage.md").is_file())
        Path("plans.md").write_text(plan([item for item in ALIGNED if item[0] != "C1"]), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cm.main(["check", "--map", "content-map.md", "--plans", "plans.md"]), 1)


if __name__ == "__main__":
    unittest.main()
