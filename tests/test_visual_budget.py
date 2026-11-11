"""Image/text budget regressions: page ratios against the confirmed base and floor,
and the budget sentence added to each brief. No network."""
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
import deck_state as ds
import extract_briefs as extract
import visual_budget as vb


def plan_page(number, ratio="85/15", source=None, anchor=None):
    note = "本页为族锚定页" if anchor is None else f"锚定页 P{anchor}"
    head = f"**[内容来源]**：{source}\n" if source else ""
    budget = f"**[融合比]**：{ratio}\n" if ratio is not None else ""
    return (f"## 第{number}页设计方案\n{head}**[场景族]**：日光（{note}）\n{budget}**[空间定位图]**\n构图与内容。\n"
            f"**可见文字清单**：\n可见文字=标题{number}；位置=左上\n")


def pages(*ratios):
    return {number: {"budget": vb.page_budget(f"**[融合比]**：{ratio}\n"), "kind": kind}
            for number, (ratio, kind) in enumerate(ratios, 1)}


class RuleTests(unittest.TestCase):
    def test_ratio_floor_and_page_field(self):
        self.assertEqual(vb.parse_ratio("85/15"), (85, 15))
        self.assertIsNone(vb.parse_ratio("85/20"))
        self.assertEqual(vb.base_and_floor({"visual_ratio": "85/15"}), (85, 80))  # 5 points below by default
        self.assertEqual(vb.base_and_floor({"visual_ratio": "85/15", "visual_floor": 82}), (85, 82))
        self.assertIsNone(vb.base_and_floor({"visual_ratio": "per-page"}))
        self.assertEqual(vb.page_budget("**[融合比]**：80/20：四要素各配一句\n**[空间定位图]**\n"),
                         {"picture": 80, "text": 20, "reason": "四要素各配一句"})
        self.assertIn("没有写", vb.page_budget("**[空间定位图]**\n**[融合比]**：85/15"))  # only the page header counts
        self.assertIn("看不懂", vb.page_budget("**[融合比]**：图多字少"))

    def test_floor_and_reason_and_two_thirds_share(self):
        ok = pages(("85/15", "cover"), ("85/15", None), ("85/15", None), ("80/20：四要素须完整", None))
        self.assertEqual(vb.check_plan(ok, 85, 80)[0], [])
        problems = vb.check_plan(pages(("75/25：太多", None), ("85/15", None)), 85, 80)[0]
        self.assertTrue(any("低于下限 80/20" in item and "拆成两页" in item for item in problems))
        problems = vb.check_plan(pages(("80/20", None), ("85/15", None), ("85/15", None)), 85, 80)[0]
        self.assertTrue(any("写明原因" in item for item in problems))
        below = pages(("85/15", None), ("80/20：a", None), ("80/20：b", None), ("85/15", None))
        problems, warnings, summary = vb.check_plan(below, 85, 80)
        self.assertTrue(any("至少要 3 页" in item for item in problems))
        self.assertEqual(summary["below_base"], [2, 3])
        problems, warnings, _ = vb.check_plan(below, 85, 80, partial=True)  # pilots only: a warning
        self.assertEqual(problems, [])
        self.assertTrue(any("至少要 3 页" in item for item in warnings))

    def test_lettering_pages_count_against_the_floor_but_not_the_share(self):
        plan = pages(("85/15", "cover"), ("85/15", "divider"), ("80/20：a", None), ("85/15", None), ("85/15", "toc"))
        problems, _, summary = vb.check_plan(plan, 85, 80)
        self.assertEqual(problems, [])
        self.assertEqual(summary["content_pages"], 3)  # toc counts, cover and divider do not
        problems = vb.check_plan(pages(("70/30：大字", "divider"), ("85/15", None)), 85, 80)[0]
        self.assertTrue(any("P1" in item and "下限" in item for item in problems))

    def test_brief_appendix_states_the_page_budget(self):
        text = vb.brief_appendix({"picture": 85, "text": 15, "reason": ""})
        self.assertIn("画面约占 85%", text)
        self.assertIn("不超过画面的 15%", text)
        self.assertEqual(vb.brief_appendix("没有写 [融合比]"), "")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppt-visual-budget-")
        self.addCleanup(self.temp.cleanup)
        self.state = str(Path(self.temp.name) / "deck-state.json")
        self.run_cli("init", "--deck-name", "t", "--requested-output", "plans")

    def run_cli(self, *args, ok=True):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ds.main([*args, "--state", self.state])
        self.assertEqual(code == 0, ok, err.getvalue() or out.getvalue())
        return out.getvalue() + err.getvalue()

    def brief(self):
        return json.loads(Path(self.state).read_text(encoding="utf-8"))["workflow"]["brief"]

    def test_floor_defaults_to_five_below_and_is_validated(self):
        self.run_cli("brief", "--visual-ratio", "85/15")
        self.assertIn("visual_ratio=85/15，下限 80/20", self.run_cli("status"))
        self.run_cli("brief", "--visual-floor", "82")
        self.assertIn("下限 82/18", self.run_cli("status"))
        self.assertIn("高于基准", self.run_cli("brief", "--visual-floor", "90", ok=False))
        self.assertIn("已取消", self.run_cli("brief", "--visual-ratio", "70/30"))  # new base, floor back to default
        self.assertNotIn("visual_floor", self.brief())
        self.assertIn("下限 65/35", self.run_cli("status"))
        self.assertIn("per-page 不设下限", self.run_cli("brief", "--visual-ratio", "per-page", "--visual-floor", "60", ok=False))

    def test_changing_the_floor_is_a_brief_change(self):
        workflow = ds.new_workflow()
        self.assertTrue(workflow["visual_budget_rules"])
        workflow["brief"]["visual_ratio"] = "85/15"
        workflow["decisions"] = {"intake": {"status": "confirmed"}}
        self.assertEqual(ds.apply_brief(workflow, {"visual_floor": 80}, "same as default"), [])
        self.assertIn("intake", workflow["decisions"])
        ds.apply_brief(workflow, {"visual_floor": 75}, "lower floor")
        self.assertNotIn("intake", workflow["decisions"])


class ExtractionTests(unittest.TestCase):
    constants = "## 全篇常量块\n画布为PPT，文字必须准确。\n"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppt-visual-budget-split-")
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
        self.workflow = {"schema_version": 1, "mode": "standard", "visual_budget_rules": True,
            "brief": {"page_count": 1, "aspect_ratio": "16:9", "audience": "test audience", "purpose": "test presentation",
                      "visual_ratio": "85/15"},
            "decisions": {"intake": dict(decision), "visual": dict(decision)},
            "artifacts": {"visual_proposal": "fixture-visual.md", "inspiration_review": "fixture-refs.md", "style_config": "fixture-style.json"},
            "typography_decision": {"specimen": "skip", "reason": "TEST ONLY: ordinary type", "summary": "TEST ONLY: five readable text roles"},
            "pilot_pages": [1]}

    def extraction(self, *plan_pages, args=()):
        content = self.constants + "".join(plan_pages)
        Path("plans.md").write_text(content)
        self.workflow["brief"]["page_count"] = max(number for number, _ in extract.split_pages(content))
        Path("deck-state.json").write_text(json.dumps({"workflow": self.workflow}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()) as err:
            code = extract.main(["plans.md", *args])
        return code, out.getvalue(), err.getvalue()

    def test_budget_reaches_every_brief_except_lettering_pages(self):
        code, out, err = self.extraction(plan_page(1, source="封面"), plan_page(2, anchor=1),
                                         plan_page(3, "80/20：四要素须完整保留", anchor=1), plan_page(4, anchor=1))
        self.assertEqual(code, 0, err)
        self.assertNotIn("图文预算", Path("briefs/p01.txt").read_text())
        self.assertIn("不超过画面的 15%", Path("briefs/p02.txt").read_text())
        self.assertIn("不超过画面的 20%", Path("briefs/p03.txt").read_text())
        report = json.loads(out)["visual_budget"]
        self.assertEqual((report["base"], report["floor"], report["below_base"]), ("85/15", "80/20", [3]))

    def test_below_floor_or_missing_budget_fails_before_output_changes(self):
        Path("manifest-pages.json").write_text("unchanged")
        code, _, err = self.extraction(plan_page(1), plan_page(2, "75/25：内容多", anchor=1))
        self.assertEqual(code, 1)
        self.assertIn("低于下限 80/20", err)
        self.assertEqual(Path("manifest-pages.json").read_text(), "unchanged")
        code, _, err = self.extraction(plan_page(1), plan_page(2, None, anchor=1))
        self.assertIn("P2 没有写 [融合比]", err)

    def test_too_many_pages_below_base(self):
        plan = [plan_page(1), plan_page(2, "80/20：a", anchor=1), plan_page(3, "80/20：b", anchor=1)]
        code, _, err = self.extraction(*plan)
        self.assertEqual(code, 1)
        self.assertIn("至少要 2 页", err)

    def test_decks_without_the_rule_flag_are_unchanged(self):
        del self.workflow["visual_budget_rules"]
        code, out, err = self.extraction(plan_page(1, "60/40"), plan_page(2, None, anchor=1))
        self.assertEqual(code, 0, err)
        self.assertNotIn("图文预算", Path("briefs/p01.txt").read_text())
        self.assertIsNone(json.loads(out)["visual_budget"])


if __name__ == "__main__":
    unittest.main()
