"""Page-to-page continuity: the three-part [承接与递进] and the 构图方式 that 变奏/亮点
pages must write for themselves. Fictional plans only; no network."""
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
import continuity
import deck_state
import extract_briefs as extract

LINK = "**[承接与递进]**\n- 承接上页：{prev}\n- 本页变化：{change}\n- 铺垫下页：{next}\n"


def plan_page(number, rhythm="稳定", layout="标题在左上，三座小岛中心约 (20%, 50%)、(50%, 50%)、(80%, 50%)。",
              prev="沿用上页三座岛", change="同骨架，只换内容", following="留出右侧的路", link=True, anchor=None):
    note = "本页为族锚定页" if anchor is None else f"锚定页 P{anchor}"
    field = LINK.format(prev=prev, change=change, next=following) if link else ""
    composition = f"**构图方式**：{layout}\n" if layout else ""
    return (f"## 第{number}页设计方案\n**[场景族]**：日光（{note}）\n**[节奏]**：{rhythm}\n{field}"
            f"**[空间定位图]**\n{composition}**可见文字清单**：\n可见文字=标题{number}；位置=左上\n")


def check(*pages, rhythms=None):
    sections = extract.split_pages("".join(pages))
    rhythms = rhythms or {number: extract.parse_rhythm(section, number) for number, section in sections}
    return continuity.check_plan(sections, rhythms)


class FieldTests(unittest.TestCase):
    def test_multi_line_and_single_line_forms(self):
        self.assertEqual(continuity.parse_field(plan_page(2)),
                         {"承接上页": "沿用上页三座岛", "本页变化": "同骨架，只换内容", "铺垫下页": "留出右侧的路"})
        single = "**[承接与递进]**：承接上页：接封面；本页变化：换成俯视；铺垫下页：引出第一章\n**[空间定位图]**\n"
        self.assertEqual(continuity.parse_field(single),
                         {"承接上页": "接封面", "本页变化": "换成俯视", "铺垫下页": "引出第一章"})
        self.assertIsNone(continuity.parse_field(plan_page(2, link=False)))
        free_text = "**[承接与递进]**：接封面，预告四章；后页进入第一章。\n**[空间定位图]**\n"
        self.assertEqual(continuity.parse_field(free_text), {})  # an unlabelled sentence names none of the three

    def test_composition_is_read_from_the_spatial_brief_only(self):
        self.assertTrue(continuity.composition(plan_page(1)).startswith("标题在左上"))
        self.assertIsNone(continuity.composition("**构图方式**：写在方案头里不算\n**[空间定位图]**\n正文"))


class RuleTests(unittest.TestCase):
    def test_a_complete_plan_passes(self):
        problems, warnings, summary = check(plan_page(1, prev="不适用"), plan_page(2), plan_page(3, following="不适用"))
        self.assertEqual((problems, warnings), ([], []))
        self.assertEqual(summary["pages"], 3)

    def test_every_page_writes_all_three_items(self):
        problems = check(plan_page(1, prev="不适用"), plan_page(2, link=False), plan_page(3, change="同上", following="不适用"))[0]
        self.assertTrue(any(item.startswith("P2 没写 [承接与递进]") for item in problems))
        self.assertIn("P3 [承接与递进] 缺“本页变化”", problems)

    def test_only_the_first_and_last_page_may_skip_a_neighbour(self):
        problems = check(plan_page(1, prev="不适用"), plan_page(2, prev="不适用"), plan_page(3, following="不适用"))[0]
        self.assertEqual(problems, ["P2 的“承接上页”写了不适用：只有第一页可以不承接、最后一页可以不铺垫"])
        problems = check(plan_page(1, prev=""), plan_page(2, following="不适用"))[0]
        self.assertEqual(problems, ["P1 [承接与递进] 缺“承接上页”（这一页可以写“不适用”）"])

    def test_stable_pages_may_share_a_skeleton_but_variation_pages_may_not(self):
        same = "标题在左上，三座小岛中心约 (20%, 50%)、(50%, 50%)、(80%, 50%)。"
        problems = check(plan_page(1, prev="不适用"), plan_page(2, layout=same), plan_page(3, layout=same, following="不适用"))[0]
        self.assertEqual(problems, [])  # parallel steps read better on one skeleton
        problems = check(plan_page(1, prev="不适用"), plan_page(2, "变奏（只变：视角）", layout=same, change="换成俯视"),
                         plan_page(3, following="不适用"))[0]
        self.assertEqual(problems, ["P2 是变奏页，构图方式和 P1 一字不差；把“本页变化”写进构图方式。"])

    def test_variation_pages_put_their_change_into_their_own_composition(self):
        problems = check(plan_page(1, prev="不适用"), plan_page(2, "亮点（核心判断）", layout=None, change="打破骨架"),
                         plan_page(3, following="不适用"))[0]
        self.assertTrue(problems[0].startswith("P2 是亮点页，空间定位图里要有一行 **构图方式**"))
        problems, warnings, _ = check(plan_page(1, prev="不适用"),
                                      plan_page(2, "变奏（只变：构图）", layout="换一个更有张力的构图。", change="换构图"),
                                      plan_page(3, following="不适用"))
        self.assertEqual(problems, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("P2（变奏页）的构图方式没写主体在哪", warnings[0])


class ShowcaseTests(unittest.TestCase):
    """封面、章节页、结尾 are showcase pages: bold, and different from one another."""

    def check(self, kinds, *pages):
        sections = extract.split_pages("".join(pages))
        return continuity.check_plan(sections, {n: extract.parse_rhythm(s, n) for n, s in sections}, kinds)

    @staticmethod
    def bold(number, layout, **kwargs):
        return plan_page(number, "亮点（展示页）", layout=layout, change="这一页自己的构图", **kwargs)

    def test_showcase_pages_are_never_stable_and_each_has_its_own_composition(self):
        kinds = {1: "cover", 2: "divider", 3: "nodes", 4: "divider", 5: "ending"}
        tall = "章节名竖排在右侧，占满画面高度；左侧是一只杯子的特写。"
        pages = [self.bold(1, "大字压在画面底部三分之一，远景是整片群岛。", prev="不适用"),
                 plan_page(2),  # a divider left at the default 稳定 inherits the content skeleton
                 plan_page(3),
                 self.bold(4, tall),
                 self.bold(5, "大字居中，俯视走完的整条路。", following="不适用")]
        problems = self.check(kinds, *pages)[0]
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("P2 是章节页，属于展示页，[节奏] 不能是稳定"))
        pages[1] = self.bold(2, tall)  # two dividers on one layout, with a content page between them
        problems = self.check(kinds, *pages)[0]
        self.assertEqual(problems, ["P4（章节页）的构图方式和 P2 一字不差。展示页各有各的构图：换景别、换大字的位置和大小、换主体在做什么。"])
        pages[1] = self.bold(2, "章节名横贯画面中线，上下各一片留白，一条路从字后面穿过。")
        problems, _, summary = self.check(kinds, *pages)
        self.assertEqual(problems, [])
        self.assertEqual(summary["showcase"], [1, 2, 4, 5])

    def test_long_runs_of_stable_content_pages_draw_a_warning(self):
        kinds = {number: "nodes" for number in range(1, 7)}
        pages = [self.bold(1, "大字压在画面底部三分之一，远景是整片群岛。", prev="不适用"),
                 *[plan_page(n) for n in range(2, 6)], plan_page(6, following="不适用")]
        problems, warnings, _ = self.check(kinds, *pages)
        self.assertEqual(problems, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("P2–P6 连续 5 页都是稳定页", warnings[0])
        plain_first = [plan_page(1, prev="不适用"), *pages[1:]]  # the first page is the cover even when its source is a node
        self.assertTrue(self.check(kinds, *plain_first)[0][0].startswith("P1 是封面，属于展示页，[节奏] 不能是稳定"))
        pages[3] = plan_page(4, "变奏（变：视角、重心）", layout="俯视，主体移到右下三分之一。", change="换成俯视")
        self.assertEqual(self.check(kinds, *pages)[1], [])  # runs of three and two
        self.assertEqual(self.check(None, *pages[:3], *[plan_page(n) for n in range(4, 6)], pages[5])[1], [])  # kinds unknown


class SplitTests(unittest.TestCase):
    constants = "## 全篇常量块\n画布为PPT，文字必须准确。\n"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppt-continuity-split-")
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
        self.workflow = {"schema_version": 1, "mode": "standard", "continuity_rules": True,
            "brief": {"page_count": 1, "aspect_ratio": "16:9", "audience": "test audience", "purpose": "test presentation"},
            "decisions": {"intake": dict(decision), "visual": dict(decision)},
            "artifacts": {"visual_proposal": "fixture-visual.md", "inspiration_review": "fixture-refs.md", "style_config": "fixture-style.json"},
            "typography_decision": {"specimen": "skip", "reason": "TEST ONLY: ordinary type", "summary": "TEST ONLY: five readable text roles"},
            "pilot_pages": [1]}

    def extraction(self, *pages):
        content = self.constants + "".join(pages)
        Path("plans.md").write_text(content)
        self.workflow["brief"]["page_count"] = max(number for number, _ in extract.split_pages(content))
        Path("deck-state.json").write_text(json.dumps({"workflow": self.workflow}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()) as err:
            code = extract.main(["plans.md"])
        return code, out.getvalue(), err.getvalue()

    def test_the_field_stays_out_of_the_brief_and_gaps_fail_the_split(self):
        code, out, err = self.extraction(plan_page(1, prev="不适用"), plan_page(2, anchor=1, following="不适用"))
        self.assertEqual(code, 0, err)
        brief = Path("briefs/p02.txt").read_text()
        self.assertNotIn("承接", brief)  # planning only: the model never sees the previous page
        self.assertIn("**构图方式**", brief)
        self.assertEqual(json.loads(out)["continuity"]["pages"], 2)
        Path("manifest-pages.json").write_text("unchanged")
        code, _, err = self.extraction(plan_page(1, prev="不适用"), plan_page(2, anchor=1, link=False))
        self.assertEqual(code, 1)
        self.assertIn("前后页衔接没写清", err)
        self.assertEqual(Path("manifest-pages.json").read_text(), "unchanged")

    def test_a_divider_sharing_the_content_title_lock_is_reported(self):
        self.constants = "## 全篇常量块\n画布为PPT，文字必须准确。\n- 字号锁 标题：字高 7%，顶 8%，左 6%，最宽 60%\n"

        def page(number, source, rhythm, layout, prev, following, anchor=None):
            note = "本页为族锚定页" if anchor is None else f"锚定页 P{anchor}"
            return (f"## 第{number}页设计方案\n**[内容来源]**：{source}\n**[场景族]**：日光（{note}）\n**[节奏]**：{rhythm}\n"
                    + LINK.format(prev=prev, change="这一页自己的构图", next=following)
                    + f"**[空间定位图]**\n**构图方式**：{layout}\n**可见文字清单**：\n可见文字=四字标题；位置=左上；锁=标题\n")

        code, out, err = self.extraction(
            page(1, "章节页 A", "亮点（章节页）", "章节名压在画面底部，远景是整片群岛。", "不适用", "引出第一节"),
            page(2, "A1", "稳定", "标题在左上，三座小岛居中。", "接章节页", "不适用", anchor=1))
        self.assertEqual(code, 0, err)
        warnings = json.loads(out)["warnings"]
        self.assertTrue(any("P1 是章节页，却套了内容页的字号锁“标题”" in warning for warning in warnings), warnings)
        self.assertFalse(any("都锁在同一个位置" in warning for warning in warnings))  # one content page is not a pattern
        many = [page(1, "章节页 A", "亮点（章节页）", "章节名压在画面底部，远景是整片群岛。", "不适用", "引出第一节")]
        many += [page(n, f"A{n - 1}", "稳定" if n % 2 else "变奏（变：重心）", f"标题在左上，主体在第 {n} 个位置。", "接上页",
                      "不适用" if n == 8 else "引出下页", anchor=1) for n in range(2, 9)]
        code, out, err = self.extraction(*many)
        self.assertEqual(code, 0, err)
        self.assertTrue(any("7 页内容页的标题都锁在同一个位置（字号锁“标题”）" in warning for warning in json.loads(out)["warnings"]))

    def test_decks_without_the_flag_are_not_checked(self):
        del self.workflow["continuity_rules"]
        code, out, err = self.extraction(plan_page(1, link=False), plan_page(2, anchor=1, link=False))
        self.assertEqual(code, 0, err)
        self.assertIsNone(json.loads(out)["continuity"])

    def test_new_decks_carry_the_flag(self):
        self.assertTrue(deck_state.new_workflow()["continuity_rules"])


if __name__ == "__main__":
    unittest.main()
