"""First-run setup: per-user config and stored keys. No network, no real keys."""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import configure
import deck_state
import generate_slide_image as generate
import local_config
import pinterest_search as search

FAKE = "TEST-ONLY-not-a-real-key-123"


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppt-setup-test-")
        self.addCleanup(self.temp.cleanup)
        env = {name: "" for name, _, _ in local_config.CREDENTIALS.values()}
        env.update({local_config.CONFIG_ENV: self.temp.name, "ZENMUX_IMAGE_MODEL": "", "ARK_IMAGE_MODEL": ""})
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        # No Keychain lookups: tests must not depend on the developer's machine.
        keychain = mock.patch.object(configure.subprocess, "run", side_effect=FileNotFoundError)
        keychain.start()
        self.addCleanup(keychain.stop)

    def run_configure(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            code = configure.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def status(self):
        return json.loads(self.run_configure("status")[1])

    def test_fresh_install_asks_for_platform_then_search(self):
        status = self.status()
        self.assertFalse(status["configured"])
        self.assertEqual(len(status["next_steps"]), 2)
        self.assertIn("豆包工作", status["next_steps"][0])
        self.assertIn("app.scrapecreators.com", status["next_steps"][1])

    def test_built_in_platforms_need_no_image_key(self):
        for platform in ("doubao", "codex"):
            with self.subTest(platform=platform):
                status = json.loads(self.run_configure("set-platform", platform)[1])
                self.assertEqual(status["image"], "built-in")
                self.assertFalse(any("生图" in step for step in status["next_steps"]))

    def test_other_platforms_must_choose_an_image_route(self):
        status = json.loads(self.run_configure("set-platform", "workbuddy")[1])
        self.assertIsNone(status["image"])
        self.assertTrue(any("生图方式" in step for step in status["next_steps"]))
        status = json.loads(self.run_configure("set-platform", "workbuddy", "--image", "seedream")[1])
        self.assertTrue(any("set-key ark" in step for step in status["next_steps"]))
        status = json.loads(self.run_configure("set-platform", "workbuddy", "--image", "built-in")[1])
        self.assertFalse(any("生图" in step for step in status["next_steps"]))

    def test_key_is_stored_privately_and_never_printed(self):
        self.run_configure("set-platform", "codex")
        code, out, err = self.run_configure("set-key", "scrapecreators", "--stdin", stdin=FAKE + "\n")
        self.assertEqual(code, 0)
        self.assertNotIn(FAKE, out + err)
        status = json.loads(out)
        self.assertTrue(status["configured"])
        self.assertEqual(status["keys"]["scrapecreators"], "config-file")
        path = Path(self.temp.name) / "credentials.json"
        self.assertEqual(json.loads(path.read_text())["scrapecreators"], FAKE)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn(FAKE, (Path(self.temp.name) / "config.json").read_text())

    def test_empty_or_spaced_key_is_rejected(self):
        for value in ("\n", "two words\n"):
            code, _, err = self.run_configure("set-key", "zenmux", "--stdin", stdin=value)
            self.assertEqual(code, 2)
        self.assertFalse((Path(self.temp.name) / "credentials.json").exists())

    def test_skip_search_is_remembered_until_a_key_arrives(self):
        self.run_configure("set-platform", "doubao")
        status = json.loads(self.run_configure("skip-search")[1])
        self.assertEqual((status["search"], status["configured"]), ("skipped", True))
        status = json.loads(self.run_configure("set-key", "scrapecreators", "--value", FAKE)[1])
        self.assertEqual(status["search"], "configured")

    def test_scripts_read_the_stored_keys_and_model(self):
        with mock.patch.object(search.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(search.load_api_key())
            local_config.write_credential("scrapecreators", FAKE)
            self.assertEqual(search.load_api_key(), FAKE)
        local_config.write_credential("ark", FAKE + "-ark")
        self.assertEqual(generate.resolve_api_key(None, "seedream"), FAKE + "-ark")
        with mock.patch.dict(os.environ, {"ARK_API_KEY": "from-env"}):
            self.assertEqual(generate.resolve_api_key(None, "seedream"), "from-env")
        self.run_configure("set-model", "gpt-image-2", "vendor/new-image-model")
        self.assertEqual(generate.default_model("gpt-image-2"), "vendor/new-image-model")
        with mock.patch.dict(os.environ, {"ZENMUX_IMAGE_MODEL": "env-model"}):
            self.assertEqual(generate.default_model("gpt-image-2"), "env-model")

    def test_provider_reason_is_shown_without_the_key(self):
        body = json.dumps({"error": {"type": "access_denied", "code": "403",
                                     "message": f"no permission for key {FAKE}"}}).encode()
        error = urllib.error.HTTPError("https://provider.invalid", 403, "Forbidden", {}, io.BytesIO(body))
        hint = generate.provider_hint(error, FAKE)
        self.assertIn("access_denied", hint)
        self.assertNotIn(FAKE, hint)
        self.assertEqual(generate.safe_error(error, FAKE), "HTTP 403")  # what the ledger keeps
        self.assertEqual(generate.provider_hint(ValueError("x")), "")


class ReopenTests(unittest.TestCase):
    def test_withdrawn_decision_moves_to_history_with_its_dependents(self):
        with tempfile.TemporaryDirectory(prefix="ppt-reopen-test-") as temp:
            state = str(Path(temp) / "deck-state.json")
            quiet = io.StringIO()
            with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                self.assertEqual(deck_state.main(["init", "--state", state, "--deck-name", "t", "--requested-output", "deck"]), 0)
                # A deck reads its material into a content skeleton before intake is confirmed.
                (Path(temp) / "source.md").write_text("讲者说：先规划，再推进。\n", encoding="utf-8")
                (Path(temp) / "content-map.md").write_text(
                    "- 类型：演讲型\n- 原文：source.md\n- 主线：TEST 先规划再推进。\n\n### A 方法\n#### A1 两步\n"
                    "- 讲者要讲什么：TEST 两步有先后。\n- 靠什么讲清楚：TEST 流程。\n- 原文依据：“先规划，再推进”\n", encoding="utf-8")
                self.assertEqual(deck_state.main(["artifact", "--state", state, "content_map", str(Path(temp) / "content-map.md")]), 0)
                deck_state.main(["brief", "--state", state, "--page-count", "8", "--aspect-ratio", "16:9",
                                 "--audience", "a", "--purpose", "p", "--visual-ratio", "80/20",
                                 "--title-layout", "uniform"])
                deck_state.main(["decide", "--state", state, "intake", "--quote", "按这组建议"])
                deck_state.main(["decide", "--state", state, "visual", "--quote", "选 A"])
                self.assertEqual(deck_state.main(["reopen", "--state", state, "visual", "--quote", "重找方向"]), 0)
            workflow = json.loads(Path(state).read_text())["workflow"]
            self.assertIn("intake", workflow["decisions"])
            self.assertNotIn("visual", workflow["decisions"])
            last = workflow["decision_history"][-1]
            self.assertEqual((last["decision"], last["status"]), ("visual", "withdrawn"))
            self.assertIn("重找方向", last["invalidated_reason"])


if __name__ == "__main__":
    unittest.main()
