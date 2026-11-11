"""deck_state.py records only what it is given and keeps the gate's view consistent.

Fictional fixtures only; no network, keys or image fees.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import deck_state as ds
import workflow_gate as gate


SOURCE = "第一节讲观念转变。好咖啡要靠稳定的注水。\n第二节讲三步法。先规划，再推进，后改善。\n"
SKELETON = """# 内容骨架（TEST ONLY）

- 类型：知识型
- 原文：source.md
- 主线：TEST 先转变观念，再用三步法。

### A 观念
#### A1 观念转变
- 讲者要讲什么：TEST 好咖啡靠稳定的注水。
- 靠什么讲清楚：TEST 一组对比。
- 原文依据：“好咖啡要靠稳定的注水”

### B 三步法
#### B1 三个步骤
- 讲者要讲什么：TEST 三步有先后。
- 靠什么讲清楚：TEST 流程。
- 原文依据：“先规划，再推进，后改善”
"""

# One document with every on-screen line of the four pages, as the user confirms it.
COPY = """# 文案稿（TEST ONLY）

## 第1页
- 内容来源：封面
- 上屏：TEST 课程名｜新增：TEST 封面标题
## 第2页
- 内容来源：A1
- 上屏：观念转变｜压缩
- 上屏：好咖啡要靠稳定的注水｜原文
- 上屏：个人与团队的对比｜压缩
## 第3页
- 内容来源：B1
- 上屏：三个步骤｜压缩
- 上屏：先规划，再推进，后改善｜原文
- 上屏：一步接一步｜压缩
## 第4页
- 内容来源：结尾
- 上屏：TEST 谢谢｜新增：TEST 结尾页
"""


class DeckStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppt-deck-state-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = str(self.root / "deck-state.json")

    def run_cli(self, *args, ok=True):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ds.main([*args, "--state", self.state])
        if ok:
            self.assertEqual(code, 0, err.getvalue())
        else:
            self.assertNotEqual(code, 0, out.getvalue())
        return out.getvalue(), err.getvalue()

    def data(self):
        return json.loads(Path(self.state).read_text(encoding="utf-8"))

    def write(self, name, text="TEST ONLY: finished artifact"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return str(path)

    def image(self, name, color="white"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 18), color).save(path)
        return str(path)

    def style(self):
        roles = {role: {"familyIntent": "TEST readable sans"} for role in gate.ROLES}
        return self.write("assets/style-configs/selected.json", json.dumps({"schemaVersion": 2, "typography": {"roles": roles}}))

    def init(self, skeleton=True, title_layout="uniform"):
        self.run_cli("init", "--deck-name", "test-deck", "--requested-output", "deck")
        if title_layout:  # ① also records how content-page titles are laid out
            self.run_cli("brief", "--title-layout", title_layout)
        if skeleton:  # a deck reads its material into a content skeleton before intake
            self.write("source.md", SOURCE)
            self.run_cli("artifact", "content_map", self.write("content-map.md", SKELETON))

    def through_visual(self):
        self.init()
        self.run_cli("brief", "--page-count", "4", "--aspect-ratio", "16:9", "--audience", "TEST readers", "--purpose", "TEST talk",
                     "--visual-ratio", "80/20")
        self.run_cli("decide", "intake", "--quote", "TEST FIXTURE: 4页16:9就行")
        self.run_cli("artifact", "page_copy", self.write("page-copy.md", COPY))
        self.run_cli("decide", "copy", "--quote", "TEST FIXTURE: 文案就这样")
        self.run_cli("artifact", "visual_proposal", self.write("visual-proposal.md"))
        self.run_cli("artifact", "inspiration_review", self.write("refs-review.md"))
        self.run_cli("artifact", "style_config", self.style())
        self.run_cli("typography", "--specimen", "skip", "--reason", "TEST plain type", "--summary", "TEST five roles shown")
        self.run_cli("decide", "visual", "--quote", "TEST FIXTURE: 选方向B")
        self.run_cli("pilot", "--pages", "1,3")

    def test_init_creates_gate_compatible_skeleton_and_refuses_overwrite(self):
        self.init()
        state = self.data()
        self.assertEqual(state["workflow"]["mode"], "standard")
        self.assertEqual(state["template_offer"]["status"], "not_offered")
        self.assertEqual(state["skill_runtime"]["version"], ds.skill_version())
        result = gate.check_workflow(self.state, "plans")
        self.assertIn("decisions.intake", result["missing"])
        self.assertNotIn("workflow.schema_version", result["missing"])
        self.run_cli("init", "--deck-name", "again", "--requested-output", "deck", ok=False)

    def test_adopt_adds_workflow_to_old_state_without_inventing_decisions(self):
        Path(self.state).write_text(json.dumps({"requested_output": "deck", "pace": "standard-user-style-selected",
                                                "completed": ["style"]}), encoding="utf-8")
        self.run_cli("status", ok=False)
        self.run_cli("init", "--deck-name", "old", "--requested-output", "deck", ok=False)  # never overwrite
        self.run_cli("init", "--adopt")
        state = self.data()
        self.assertEqual(state["completed"], ["style"])  # old fields kept as history
        self.assertEqual(state["workflow"]["decisions"], {})
        self.assertIn("decisions.intake", gate.check_workflow(self.state, "plans")["missing"])
        self.run_cli("init", "--adopt", ok=False)  # already adopted

    def test_decision_records_pass_the_gate_user_record_check(self):
        self.through_visual()
        decisions = self.data()["workflow"]["decisions"]
        for name in ("intake", "visual"):
            self.assertTrue(gate.user_record(decisions[name], decision=True))
        self.assertTrue(gate.check_workflow(self.state, "plans")["ok"])

    def test_intake_needs_complete_brief_and_proposals_need_acceptance(self):
        self.init()
        self.run_cli("brief", "--page-count", "10", "--aspect-ratio", "16:9", "--visual-ratio", "60/40", "--proposed")
        self.assertIsNone(self.data()["workflow"]["brief"]["page_count"])
        self.assertIsNone(self.data()["workflow"]["brief"]["visual_ratio"])
        self.run_cli("decide", "intake", "--quote", "按这组建议", "--apply-proposal", ok=False)  # audience/purpose missing
        self.run_cli("brief", "--audience", "TEST investors", "--purpose", "TEST pitch")
        self.run_cli("decide", "intake", "--quote", "按这组建议", "--apply-proposal")
        brief = self.data()["workflow"]["brief"]
        self.assertEqual((brief["page_count"], brief["aspect_ratio"], brief["visual_ratio"]), (10, "16:9", "60/40"))
        self.assertNotIn("brief", self.data()["workflow"]["proposals"])

    def test_user_values_supersede_unaccepted_proposal(self):
        self.init()
        self.run_cli("brief", "--page-count", "5")
        self.run_cli("brief", "--aspect-ratio", "16:9", "--audience", "TEST a", "--purpose", "TEST p", "--proposed")
        self.run_cli("brief", "--aspect-ratio", "21:9", "--audience", "TEST a", "--purpose", "TEST p2", "--visual-ratio", "per-page")
        self.run_cli("decide", "intake", "--quote", "TEST FIXTURE: 21:9")
        workflow = self.data()["workflow"]
        self.assertEqual(workflow["brief"]["aspect_ratio"], "21:9")
        self.assertNotIn("brief", workflow["proposals"])
        self.assertEqual(workflow["proposal_history"][0]["brief"]["aspect_ratio"], "16:9")

    def test_changing_confirmed_brief_invalidates_intake_and_pilot(self):
        self.through_visual()
        for page in (1, 3):
            self.run_cli("artifact", "pilot_image", self.image(f"images/p0{page}.png"), "--page", str(page))
        self.run_cli("decide", "pilot", "--quote", "TEST FIXTURE: 样页可以")
        _, err = self.run_cli("brief", "--page-count", "6")
        decisions = self.data()["workflow"]["decisions"]
        self.assertNotIn("intake", decisions)
        self.assertNotIn("pilot", decisions)
        self.assertIn("visual", decisions)
        self.assertIn("重新向用户展示", err)
        history = self.data()["workflow"]["decision_history"]
        self.assertEqual({row["decision"] for row in history if row["status"] == "invalidated"}, {"intake", "copy", "pilot"})
        # Re-sending the same value is not a change.
        self.run_cli("decide", "intake", "--quote", "TEST FIXTURE: 改成6页")
        self.run_cli("brief", "--page-count", "6")
        self.assertIn("intake", self.data()["workflow"]["decisions"])

    def test_visual_ratio_is_validated_and_normalized(self):
        self.init()
        for given, stored in (("80/20", "80/20"), (" 60 / 40 ", "60/40"), ("50/50", "50/50"),
                              ("PER-PAGE", "per-page"), ("per-page", "per-page")):
            self.run_cli("brief", "--visual-ratio", given)
            self.assertEqual(self.data()["workflow"]["brief"]["visual_ratio"], stored)
        for bad in ("80/30", "100/0", "0/100", "80:20", "80", "八二", "", "1/2/97"):
            self.run_cli("brief", "--visual-ratio", bad, ok=False)
        self.assertEqual(self.data()["workflow"]["brief"]["visual_ratio"], "per-page")

    def test_intake_requires_visual_ratio_in_the_brief(self):
        self.init()
        self.run_cli("brief", "--page-count", "8", "--aspect-ratio", "16:9", "--audience", "TEST a", "--purpose", "TEST p")
        out, _ = self.run_cli("status")
        self.assertIn("visual_ratio=（缺，默认 80/20）", out)
        _, err = self.run_cli("decide", "intake", "--quote", "TEST FIXTURE: 8页16:9", ok=False)
        self.assertIn("visual_ratio", err)
        self.assertIn("80/20", err)
        self.assertNotIn("intake", self.data()["workflow"]["decisions"])
        self.run_cli("brief", "--visual-ratio", "80/20", "--proposed")
        self.assertIn("visual_ratio=（缺，默认 80/20）", self.run_cli("status")[0])  # still only a suggestion
        self.run_cli("decide", "intake", "--quote", "按这组建议", "--apply-proposal")
        self.assertEqual(self.data()["workflow"]["brief"]["visual_ratio"], "80/20")
        self.assertIn("visual_ratio=80/20", self.run_cli("status")[0])

    def test_intake_requires_the_users_choice_of_title_layout(self):
        self.init(title_layout=None)
        self.run_cli("brief", "--page-count", "8", "--aspect-ratio", "16:9", "--audience", "TEST a", "--purpose", "TEST p",
                     "--visual-ratio", "80/20")
        self.assertIn("title_layout=（缺）", self.run_cli("status")[0])
        _, err = self.run_cli("decide", "intake", "--quote", "TEST FIXTURE: 8页16:9", ok=False)
        self.assertIn("title_layout", err)
        self.assertIn("统一版式", err)
        self.assertIn("自由构图", err)
        _, err = self.run_cli("brief", "--title-layout", "随便", ok=False)
        self.assertIn("uniform", err)
        self.run_cli("brief", "--title-layout", "自由构图")  # the Chinese name is accepted
        self.assertEqual(self.data()["workflow"]["brief"]["title_layout"], "free")
        self.assertIn("title_layout=自由构图", self.run_cli("status")[0])
        self.run_cli("decide", "intake", "--quote", "TEST FIXTURE: 自由构图")
        self.assertIn("intake", self.data()["workflow"]["decisions"])

    def test_changing_title_layout_withdraws_what_was_confirmed_on_it(self):
        self.through_visual()
        self.run_cli("brief", "--title-layout", "uniform")  # same value: no change
        self.assertIn("intake", self.data()["workflow"]["decisions"])
        _, err = self.run_cli("brief", "--title-layout", "free")
        self.assertNotIn("intake", self.data()["workflow"]["decisions"])
        self.assertIn("重新向用户展示", err)

    def test_states_created_before_the_title_layout_choice_are_not_blocked(self):
        self.init(title_layout=None)
        state = self.data()
        del state["workflow"]["title_layout_rules"]
        del state["workflow"]["brief"]["title_layout"]
        Path(self.state).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        self.run_cli("brief", "--page-count", "8", "--aspect-ratio", "16:9", "--audience", "TEST a", "--purpose", "TEST p",
                     "--visual-ratio", "80/20")
        self.assertNotIn("title_layout", self.run_cli("status")[0])
        self.run_cli("decide", "intake", "--quote", "TEST FIXTURE: 8页16:9")
        self.assertIn("intake", self.data()["workflow"]["decisions"])

    def test_changing_visual_ratio_invalidates_intake_and_pilot(self):
        self.through_visual()
        for page in (1, 3):
            self.run_cli("artifact", "pilot_image", self.image(f"images/p0{page}.png"), "--page", str(page))
        self.run_cli("decide", "pilot", "--quote", "TEST FIXTURE: 样页可以")
        self.run_cli("brief", "--visual-ratio", "80/20")  # same value: no change
        self.assertIn("intake", self.data()["workflow"]["decisions"])
        _, err = self.run_cli("brief", "--visual-ratio", "50/50")
        decisions = self.data()["workflow"]["decisions"]
        self.assertNotIn("intake", decisions)
        self.assertNotIn("pilot", decisions)
        self.assertIn("visual", decisions)
        self.assertIn("重新向用户展示", err)

    def test_legacy_intake_without_ratio_counts_as_default(self):
        self.through_visual()

        def drop_ratio():  # a state that confirmed intake without this field
            state = self.data()
            state["workflow"]["brief"].pop("visual_ratio")
            Path(self.state).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        drop_ratio()
        self.assertTrue(gate.check_workflow(self.state, "plans")["ok"])  # the gate does not require it
        warnings = json.loads(self.run_cli("status", "--json")[0])["warnings"]
        self.assertTrue(any("图文融合占比" in w for w in warnings))
        self.run_cli("brief", "--visual-ratio", "80/20")  # same as the default it was built on
        self.assertIn("intake", self.data()["workflow"]["decisions"])
        warnings = json.loads(self.run_cli("status", "--json")[0])["warnings"]
        self.assertFalse(any("图文融合占比" in w for w in warnings))
        drop_ratio()
        self.run_cli("brief", "--visual-ratio", "per-page")
        self.assertNotIn("intake", self.data()["workflow"]["decisions"])

    def test_changing_typography_invalidates_visual(self):
        self.through_visual()
        self.run_cli("typography", "--specimen", "generate", "--reason", "TEST expressive title", "--summary", "TEST new plan")
        self.assertNotIn("visual", self.data()["workflow"]["decisions"])

    def test_pilot_confirmation_requires_real_images(self):
        self.through_visual()
        self.run_cli("decide", "pilot", "--quote", "TEST FIXTURE: 可以", ok=False)
        self.run_cli("artifact", "pilot_image", self.write("images/p01.png", "not an image"), "--page", "1", ok=False)
        self.run_cli("artifact", "pilot_image", self.image("images/p01.png"), "--page", "1")
        self.run_cli("decide", "pilot", "--quote", "TEST FIXTURE: 可以", ok=False)  # P3 still missing
        self.run_cli("artifact", "pilot_image", self.image("images/p03.png"), "--page", "3")
        self.run_cli("artifact", "pilot_review", self.write("pilot-review.md"))
        self.run_cli("decide", "pilot", "--quote", "TEST FIXTURE: 样页可以")
        self.assertTrue(gate.check_workflow(self.state, "production")["ok"])
        self.assertEqual(set(self.data()["workflow"]["decisions"]["pilot"]["images_sha256"]), {"1", "3"})

    def test_status_reports_next_steps_and_drift(self):
        self.through_visual()
        out, _ = self.run_cli("status")
        self.assertIn("生成样页(pilot) ✓", out)  # skip specimen: pilot may start
        self.assertIn("生成余页（production）前还需要", out)
        for page in (1, 3):
            self.run_cli("artifact", "pilot_image", self.image(f"images/p0{page}.png"), "--page", str(page))
        self.run_cli("artifact", "pilot_review", self.write("pilot-review.md"))
        self.run_cli("decide", "pilot", "--quote", "TEST FIXTURE: 样页可以")
        out, _ = self.run_cli("status")
        self.assertIn("可以生成余页", out)
        self.image("images/p03.png", "black")  # regenerated after confirmation
        self.write("assets/style-configs/selected.json", "{}")
        summary = json.loads(self.run_cli("status", "--json")[0])
        self.assertTrue(any("P3" in w for w in summary["warnings"]))
        self.assertTrue(any("style_config" in w for w in summary["warnings"]))

    def test_intake_needs_a_checked_content_skeleton(self):
        self.init(skeleton=False)
        self.run_cli("brief", "--page-count", "4", "--aspect-ratio", "16:9", "--audience", "TEST a", "--purpose", "TEST p",
                     "--visual-ratio", "70/30")
        _, err = self.run_cli("decide", "intake", "--quote", "TEST FIXTURE: 4页", ok=False)
        self.assertIn("内容骨架", err)
        self.assertIn("artifacts.content_map", gate.check_workflow(self.state, "plans")["missing"])
        self.assertIn("内容骨架：（未登记）", self.run_cli("status")[0])
        self.write("source.md", SOURCE)
        # A quotation that is not in the source is the agent's own wording, not evidence.
        invented = SKELETON.replace("“好咖啡要靠稳定的注水”", "“好咖啡全靠运气”")
        _, err = self.run_cli("artifact", "content_map", self.write("content-map.md", invented), ok=False)
        self.assertIn("在原文里找不到", err)
        self.assertNotIn("content_map", self.data()["workflow"]["artifacts"])
        self.run_cli("artifact", "content_map", self.write("content-map.md", SKELETON))
        self.run_cli("decide", "intake", "--quote", "TEST FIXTURE: 4页")
        self.assertIn("content_map", self.data()["workflow"]["decisions"]["intake"]["artifacts_sha256"])
        self.assertIn("内容骨架：知识型 · 2 个部分 2 节 · 不上屏 0 节", self.run_cli("status")[0])
        # Changing the skeleton after the user confirmed it is reported, not hidden.
        self.write("content-map.md", SKELETON.replace("TEST 流程。", "TEST 流程。\n- 不上屏：TEST 讲师口述"))
        summary = json.loads(self.run_cli("status", "--json")[0])
        self.assertTrue(any("内容骨架已改变" in w for w in summary["warnings"]))
        self.assertIn("不上屏 1 节", summary["content_map"])

    def test_states_created_before_the_skeleton_are_not_blocked(self):
        self.init(skeleton=False)
        state = self.data()
        state["workflow"].pop("content_map_required")  # a state without the flag
        Path(self.state).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        self.run_cli("brief", "--page-count", "4", "--aspect-ratio", "16:9", "--audience", "TEST a", "--purpose", "TEST p",
                     "--visual-ratio", "80/20")
        self.run_cli("decide", "intake", "--quote", "TEST FIXTURE: 4页")
        self.assertNotIn("artifacts.content_map", gate.check_workflow(self.state, "plans")["missing"])
        self.assertIn("旧作品，没有内容骨架", self.run_cli("status")[0])

    def confirmed_intake(self):
        self.init()
        self.run_cli("brief", "--page-count", "4", "--aspect-ratio", "16:9", "--audience", "TEST a", "--purpose", "TEST p",
                     "--visual-ratio", "70/30")
        self.run_cli("decide", "intake", "--quote", "TEST FIXTURE: 4页")

    def test_copy_sheet_is_shown_and_confirmed_before_any_design(self):
        self.confirmed_intake()
        missing = gate.check_workflow(self.state, "plans")["missing"]
        self.assertIn("decisions.copy", missing)
        self.assertIn("artifacts.page_copy", missing)
        self.assertIn("文案稿：（未登记）", self.run_cli("status")[0])
        _, err = self.run_cli("decide", "copy", "--quote", "TEST FIXTURE: 可以", ok=False)  # nothing was shown yet
        self.assertIn("文案稿", err)
        # A quotation is not put on a knowledge deck unless the user asked for it; an invented line needs a reason.
        slogan = COPY.replace("- 上屏：一步接一步｜压缩", "- 上屏：先规划，再推进，后改善｜引语\n- 上屏：TEST 行动起来｜新增")
        _, err = self.run_cli("artifact", "page_copy", self.write("page-copy.md", slogan), ok=False)
        self.assertIn("默认不上屏", err)
        self.assertIn("要写理由", err)
        self.assertNotIn("page_copy", self.data()["workflow"]["artifacts"])
        out, _ = self.run_cli("artifact", "page_copy", self.write("page-copy.md", COPY))
        self.assertIn("4 页 8 行上屏文字：原文 2、压缩 4、新增 2、引语 0", out)
        self.run_cli("decide", "copy", "--quote", "TEST FIXTURE: 文案就这样")
        self.assertIn("page_copy", self.data()["workflow"]["decisions"]["copy"]["artifacts_sha256"])
        self.assertNotIn("decisions.copy", gate.check_workflow(self.state, "plans")["missing"])
        self.assertIn("文案稿 用户确认", self.run_cli("status")[0])

    def test_copy_changed_after_confirmation_blocks_until_shown_again(self):
        self.through_visual()
        self.assertTrue(gate.check_workflow(self.state, "plans")["ok"])
        self.write("page-copy.md", COPY.replace("一步接一步", "一步接着一步"))
        result = gate.check_workflow(self.state, "plans")
        self.assertEqual(result["missing"], ["decisions.copy.changed"])
        self.assertTrue(any("文案稿在用户确认后改过" in w for w in json.loads(self.run_cli("status", "--json")[0])["warnings"]))
        self.run_cli("decide", "copy", "--quote", "TEST FIXTURE: 改后的也可以")
        self.assertTrue(gate.check_workflow(self.state, "plans")["ok"])

    def test_page_count_or_skeleton_change_withdraws_the_copy_confirmation(self):
        self.through_visual()
        _, err = self.run_cli("brief", "--page-count", "5")
        decisions = self.data()["workflow"]["decisions"]
        self.assertNotIn("copy", decisions)
        self.assertIn("visual", decisions)
        self.assertIn("文案稿", err)
        self.assertIn("已确认的页数是 5 页", " ".join(gate.check_workflow(self.state, "plans")["next_steps"]))

    def test_reopening_intake_withdraws_copy_and_delegation_can_cover_it(self):
        self.through_visual()
        self.run_cli("reopen", "copy", "--quote", "TEST FIXTURE: 文案我再想想")
        decisions = self.data()["workflow"]["decisions"]
        self.assertNotIn("copy", decisions)
        self.assertIn("intake", decisions)
        self.run_cli("delegate", "--quote", "TEST FIXTURE: 文案你定，不用问我", "--scopes", "copy")
        result = gate.check_workflow(self.state, "plans")
        self.assertTrue(result["ok"], result["missing"])  # the sheet is still written and checked
        self.assertEqual(result["authorized_by"]["copy"], "explicit_delegation")
        Path(self.root / "page-copy.md").unlink()
        self.assertIn("artifacts.page_copy", gate.check_workflow(self.state, "plans")["missing"])

    def test_states_without_the_copy_step_are_not_blocked(self):
        self.confirmed_intake()
        state = self.data()
        state["workflow"].pop("page_copy_required")
        Path(self.state).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        missing = gate.check_workflow(self.state, "plans")["missing"]
        self.assertNotIn("decisions.copy", missing)
        self.assertNotIn("artifacts.page_copy", missing)
        self.assertIn("文案稿：（这套作品没有这一步）", self.run_cli("status")[0])

    def test_non_deck_scope_skips_gates(self):
        self.run_cli("init", "--deck-name", "refs", "--requested-output", "inspiration")
        out, _ = self.run_cli("status")
        self.assertIn("不经过整套生成门禁", out)
        self.assertEqual(json.loads(self.run_cli("status", "--json")[0])["gates"], {})

    def test_delegation_rejects_bare_defaults_and_records_scopes(self):
        self.init()
        self.run_cli("delegate", "--quote", "默认", "--scopes", "intake,visual,pilot", ok=False)
        self.run_cli("delegate", "--quote", "TEST FIXTURE: 页数风格你定", "--scopes", "intake,bogus", ok=False)
        self.run_cli("delegate", "--quote", "TEST FIXTURE: 页数风格你定，直接做", "--scopes", "intake,visual")
        workflow = self.data()["workflow"]
        self.assertEqual(workflow["mode"], "autonomous")
        self.assertTrue(gate.user_record(workflow["delegation"]))
        self.run_cli("delegate", "--revoke")
        self.assertEqual(self.data()["workflow"]["mode"], "standard")

    def test_artifact_must_exist_and_paths_are_relative_to_state(self):
        self.init()
        self.run_cli("artifact", "visual_proposal", str(self.root / "missing.md"), ok=False)
        self.write("visual-proposal.md")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        cwd = os.getcwd()
        os.chdir(elsewhere)
        try:
            self.run_cli("artifact", "visual_proposal", "../visual-proposal.md")
        finally:
            os.chdir(cwd)
        self.assertEqual(self.data()["workflow"]["artifacts"]["visual_proposal"], "visual-proposal.md")

    def test_pilot_pages_validated_against_brief(self):
        self.init()
        self.run_cli("brief", "--page-count", "4")
        for pages in ("1,2,3,4", "1,1", "0", "5", "x"):
            self.run_cli("pilot", "--pages", pages, ok=False)
        self.run_cli("pilot", "--pages", "1,4")

    def test_template_offer_is_asked_once_and_saved_needs_consent(self):
        self.init()
        self.run_cli("template-offer", "saved", "--template-id", "t1", "--version", "v001", ok=False)
        self.run_cli("template-offer", "offered", "--candidate-file", "template-candidate.json")
        self.run_cli("template-offer", "offered", ok=False)
        self.run_cli("template-offer", "declined", ok=False)  # needs the user's words
        self.run_cli("template-offer", "declined", "--quote", "TEST FIXTURE: 这次不保存")
        self.run_cli("template-offer", "offered", ok=False)
        self.run_cli("template-offer", "offered", "--user-requested")
        self.run_cli("template-offer", "approved", "--quote", "TEST FIXTURE: 存成暖白编辑风")
        self.run_cli("template-offer", "saved", "--template-id", "t1", "--version", "v001")
        self.assertEqual(self.data()["template_offer"]["status"], "saved")


if __name__ == "__main__":
    unittest.main()
