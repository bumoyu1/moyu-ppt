"""Behavior gates use fictional decision fixtures only; no network or image fees."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

# Keep tests off any real per-user config or stored keys.
os.environ.setdefault("MOYU_PPT_CONFIG_DIR", tempfile.mkdtemp(prefix="ppt-config-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import extract_briefs as extract
import generate_slide_image as generate
import workflow_gate as gate


def decision(evidence="TEST FIXTURE: I confirm the displayed proposal"):
    return {"status": "confirmed", "source": "user", "evidence": evidence,
            "confirmed_at": "2026-09-09T10:00:00+08:00"}


def fixture_workflow():
    return {"schema_version": 1, "mode": "standard",
        "brief": {"page_count": 4, "aspect_ratio": "16:9", "audience": "TEST readers", "purpose": "TEST talk"},
        "decisions": {"intake": decision(), "visual": decision()},
        "artifacts": {"visual_proposal": "visual-proposal.md", "inspiration_review": "refs-review.md", "style_config": "style.json"},
        "typography_decision": {"specimen": "skip", "reason": "TEST ordinary readable type", "summary": "TEST five roles shown in the proposal"},
        "pilot_pages": [1, 3]}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppt-workflow-tests-")
        self.cwd = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(os.chdir, self.cwd)
        self.workflow = fixture_workflow()
        self.write_state()
        for name in ("visual-proposal.md", "refs-review.md", "pilot-review.md"):
            Path(name).write_text("TEST ONLY: completed review artifact", encoding="utf-8")
        self.style = {"schemaVersion": 2, "typography": {"roles": {role: {"familyIntent": "TEST readable sans"} for role in gate.ROLES}}}
        self.write_style()
        self.network = mock.patch.object(generate.urllib.request, "urlopen", side_effect=AssertionError("Network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def write_state(self):
        Path("deck-state.json").write_text(json.dumps({"workflow": self.workflow}), encoding="utf-8")

    def write_style(self):
        Path("style.json").write_text(json.dumps(self.style), encoding="utf-8")

    def check(self, stage="plans", **kwargs):
        self.write_state()
        return gate.check_workflow("deck-state.json", stage, **kwargs)

    def pilot_passed(self):
        self.workflow["decisions"]["pilot"] = decision()
        self.workflow["artifacts"]["pilot_review"] = "pilot-review.md"
        images = {}
        for page in self.workflow["pilot_pages"]:
            path = f"pilot-{page}.png"
            Image.new("RGB", (32, 18), "white").save(path)
            images[str(page)] = path
        self.workflow["artifacts"]["pilot_images"] = images

    def batch(self, pages, *args, **job_fields):
        jobs = [{"page": page, "prompt": "TEST slide", "output": f"images/p{index:02d}.png", **job_fields}
                for index, page in enumerate(pages, 1)]
        Path("jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
        stderr = io.StringIO()
        with mock.patch.object(generate, "resolve_api_key", side_effect=AssertionError("Key read forbidden")), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            status = generate.main(["--batch", "jobs.json", "--dry-run", "--export-inputs", "native-inputs.json", *args])
        return status, stderr.getvalue()

    def test_missing_and_legacy_state_never_acquire_authority(self):
        Path("deck-state.json").unlink()
        self.assertIn("workflow_state", gate.check_workflow()["missing"])
        for legacy in ({"pace": "fast", "page_count": 12}, {"pace": "autonomous", "status": "confirmed"}):
            Path("deck-state.json").write_text(json.dumps(legacy))
            self.assertIn("workflow", gate.check_workflow()["missing"])

    def test_style_selection_is_not_intake_confirmation(self):
        del self.workflow["decisions"]["intake"]
        result = self.check()
        self.assertIn("decisions.intake", result["missing"])
        self.assertNotIn("decisions.visual", result["missing"])

    def test_defaults_silence_and_legacy_pace_do_not_delegate(self):
        self.workflow["decisions"] = {}
        for extra in ({"pace": "fast"}, {"pace": "autonomous"}, {"default": True}, {"silent": True}):
            self.workflow.update(extra)
            result = self.check()
            self.assertIn("decisions.intake", result["missing"])
        self.workflow["mode"] = "autonomous"
        for evidence in ("默认", "default", "沉默", "", "继续", "ok"):
            self.workflow["delegation"] = {**decision(evidence), "scopes": ["intake", "visual", "pilot"]}
            result = self.check()
            self.assertIn("delegation", result["missing"])
            self.assertIn("decisions.intake", result["missing"])

    def test_decision_needs_actual_source_evidence_and_timezone_shape(self):
        for changes in ({"source": "assistant"}, {"evidence": " "}, {"status": "proposed"},
                        {"confirmed_at": "2026-09-09T10:00:00"}, {"confirmed_at": "bad"}):
            self.workflow["decisions"]["intake"] = {**decision(), **changes}
            self.assertIn("decisions.intake", self.check()["missing"])

    def test_standard_flow_allows_pilot_but_blocks_production(self):
        self.assertTrue(self.check("plans")["ok"])
        self.assertTrue(self.check("pilot", pages=[1, 3], aspect_ratios=["16:9", "32:18"])["ok"])
        result = self.check("production", pages=[1, 2, 3, 4])
        self.assertIn("decisions.pilot", result["missing"])
        self.assertIn("artifacts.pilot_review", result["missing"])
        self.pilot_passed()
        self.assertTrue(self.check("production", pages=[2, 4])["ok"])

    def test_font_roles_and_user_visible_decision_are_required(self):
        for empty in ({}, {"familyIntent": " "}, None):
            self.style["typography"]["roles"]["body"] = empty
            self.write_style()
            self.assertIn("typography.roles.body", self.check()["missing"])
        self.style["typography"]["roles"]["body"] = {"familyIntent": "readable"}
        self.write_style()
        for field in ("specimen", "reason", "summary"):
            saved = self.workflow["typography_decision"].pop(field)
            self.assertIn(f"typography_decision.{field}", self.check()["missing"])
            self.workflow["typography_decision"][field] = saved

    def test_specimen_is_needed_for_pilot_but_not_before_plans(self):
        self.workflow["typography_decision"]["specimen"] = "generate"
        self.assertTrue(self.check("plans")["ok"])
        self.assertIn("artifacts.typography_specimen", self.check("pilot")["missing"])
        self.workflow["artifacts"]["typography_specimen"] = "type.png"
        Path("type.png").write_text("not actually an image")
        self.assertIn("artifacts.typography_specimen", self.check("pilot")["missing"])
        Image.new("RGB", (20, 10), "white").save("type.png")
        for mode in ("generate", "provided"):
            self.workflow["typography_decision"]["specimen"] = mode
            self.assertTrue(self.check("pilot")["ok"])

    def test_delegation_scopes_do_not_skip_artifacts_fonts_or_limits(self):
        self.pilot_passed()
        self.workflow["mode"] = "autonomous"
        self.workflow["decisions"] = {}
        self.workflow["delegation"] = {"source": "user", "evidence": "TEST FIXTURE: decide the intake, visual direction and pilot for me",
            "scopes": ["intake", "visual", "pilot"], "confirmed_at": "2026-09-09T10:00:00+08:00"}
        self.workflow["artifacts"]["pilot_review"] = "pilot-review.md"
        self.assertTrue(self.check("production", pages=[1, 4])["ok"])
        self.workflow["delegation"]["scopes"] = ["intake"]
        self.assertIn("decisions.visual", self.check("pilot")["missing"])
        self.workflow["delegation"]["scopes"] = ["intake", "visual", "pilot"]
        self.workflow["typography_decision"]["summary"] = ""
        Path("refs-review.md").unlink()
        result = self.check("production", pages=[5])
        self.assertIn("typography_decision.summary", result["missing"])
        self.assertIn("artifacts.inspiration_review", result["missing"])
        self.assertIn("batch.page_count", result["missing"])

    def test_production_requires_decodable_images_for_every_pilot_page(self):
        self.pilot_passed()
        del self.workflow["artifacts"]["pilot_images"]["3"]
        self.assertIn("artifacts.pilot_images.3", self.check("production")["missing"])
        self.workflow["artifacts"]["pilot_images"]["3"] = "pilot-3.png"
        Path("pilot-3.png").write_text("not an image")
        self.assertIn("artifacts.pilot_images.3", self.check("production")["missing"])
        Image.new("RGB", (32, 18), "white").save("pilot-3.png")
        result = self.check("production")
        self.assertTrue(result["ok"])
        self.assertEqual(set(result["image_evidence"]), {"pilot.1", "pilot.3"})

    def test_artifacts_resolve_relative_to_state_in_another_directory(self):
        self.write_state()
        elsewhere = Path("elsewhere")
        elsewhere.mkdir()
        os.chdir(elsewhere)  # contextlib.chdir needs 3.11; keep 3.9 compatible
        try:
            self.assertTrue(gate.check_workflow("../deck-state.json")["ok"])
        finally:
            os.chdir(self.temp.name)
        Path("visual-proposal.md").write_text(" \n")
        self.assertIn("artifacts.visual_proposal", gate.check_workflow()["missing"])

    def test_page_and_ratio_limits_are_enforced_without_forbidding_subsets(self):
        self.assertTrue(self.check(pages=[1, 4], aspect_ratios=["32:18"])["ok"])
        for pages in ([True], [1.5], [0], [1, 1], [None], []):
            self.assertIn("batch.pages", self.check(pages=pages)["missing"])
        self.assertIn("batch.page_count", self.check(pages=[5])["missing"])
        self.assertIn("plans.page_count", self.check(pages=[1, 3], require_complete=True)["missing"])
        self.assertIn("batch.aspect_ratio", self.check(aspect_ratios=["21:9"])["missing"])
        self.assertIn("batch.pilot_pages", self.check("pilot", pages=[2])["missing"])
        for pages in ([], [1, 2, 3, 4], [1, 1], [5], [True]):
            self.workflow["pilot_pages"] = pages
            self.assertIn("pilot_pages", self.check()["missing"])

    def test_sanitized_twelve_page_failure_blocks_cli_before_exports_or_keys(self):
        # Sanitized shape of the reported failure: twelve pages and style
        # selection existed, but no actual intake acceptance was recorded.
        self.workflow["brief"]["page_count"] = 12
        self.workflow["decisions"] = {"visual": decision("TEST FIXTURE: use direction 2")}
        self.write_state()
        status, stderr = self.batch(list(range(1, 13)))
        self.assertEqual(status, 1)
        self.assertIn("decisions.intake", stderr)
        self.assertFalse(Path("native-inputs.json").exists())
        self.assertFalse(Path("run-state.json").exists())

    def test_cli_missing_legacy_or_mixed_state_cannot_force_bypass(self):
        for state in (None, {"pace": "autonomous", "page_count": 12}):
            if state is None:
                Path("deck-state.json").unlink(missing_ok=True)
            else:
                Path("deck-state.json").write_text(json.dumps(state))
            status, stderr = self.batch([1], "--generation-stage", "pilot", "--force")
            self.assertEqual(status, 1)
            self.assertIn("Workflow gate blocked", stderr)
            self.assertFalse(Path("native-inputs.json").exists())
            self.assertFalse(Path("run-state.json").exists())
        self.write_state()
        status, stderr = self.batch([1, None], "--generation-stage", "pilot")
        self.assertEqual(status, 1)
        self.assertIn("batch.pages", stderr)

    def test_cli_native_pilot_is_limited_and_production_waits(self):
        status, _ = self.batch([1, 3], "--generation-stage", "pilot")
        self.assertEqual(status, 0)
        self.assertEqual(len(json.loads(Path("native-inputs.json").read_text())["jobs"]), 2)
        Path("native-inputs.json").unlink()
        status, stderr = self.batch([1, 2], "--generation-stage", "pilot")
        self.assertEqual(status, 1)
        self.assertIn("batch.pilot_pages", stderr)
        self.assertFalse(Path("native-inputs.json").exists())
        status, stderr = self.batch([1, 3])
        self.assertEqual(status, 1)
        self.assertIn("decisions.pilot", stderr)

    def test_cli_production_checks_actual_dimensions_as_well_as_ratio_label(self):
        self.pilot_passed()
        self.write_state()
        self.assertEqual(self.batch([2, 4])[0], 0)
        Path("native-inputs.json").unlink()
        for fields in ({"aspect_ratio": "21:9"}, {"size": "1280x960"}):
            status, stderr = self.batch([2], **fields)
            self.assertEqual(status, 1)
            self.assertIn("batch.aspect_ratio", stderr)
        self.assertEqual(self.batch([2], size="1600x900")[0], 0)
        self.assertEqual(self.batch([5])[0], 1)

    def test_independent_specimen_batch_does_not_need_deck_state(self):
        Path("deck-state.json").unlink()
        Path("specimen.json").write_text(json.dumps([{"prompt": "TEST title specimen", "output": "title.png"}]))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(generate.main(["--batch", "specimen.json", "--dry-run"]), 0)
        self.assertFalse(Path("run-state.json").exists())

    def test_extract_missing_decision_preserves_existing_output(self):
        Path("manifest-anchors.json").write_text("old output")
        Path("plans.md").write_text("not read because workflow blocks")
        del self.workflow["decisions"]["intake"]
        self.write_state()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(extract.main(["plans.md"]), 1)
        self.assertEqual(Path("manifest-anchors.json").read_text(), "old output")

    def test_extract_requires_confirmed_page_count_and_approved_style(self):
        plan = "## 全篇常量块\nTEST complete constants\n"
        for page in (1, 2):
            plan += (f"## 第{page}页设计方案\n**[场景族]**：TEST{page}（本页为族锚定页）\n"
                     "**[空间定位图]**\nTEST composition\n**可见文字清单**\n可见文字=TEST title；位置=left\n")
        Path("plans.md").write_text(plan)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(extract.main(["plans.md"]), 1)
            self.assertFalse(Path("manifest-anchors.json").exists())
            self.assertEqual(extract.main(["plans.md", "--allow-partial"]), 0)
            self.assertEqual(extract.main(["plans.md", "--style-config", "unreviewed.json", "--allow-partial"]), 1)

    def test_gate_cli_is_read_only_and_gives_actionable_next_steps(self):
        before = Path("deck-state.json").read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(gate.main(["check", "--stage", "production"]), 1)
        report = json.loads(output.getvalue())
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["missing"]), len(report["next_steps"]))
        self.assertIn("decisions.pilot", report["missing"])
        self.assertEqual(Path("deck-state.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
