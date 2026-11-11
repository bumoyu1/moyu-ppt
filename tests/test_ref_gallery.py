"""Reference gallery and library: mechanical selection, numbering, the user's picks and
the local library. No network; downloads are replaced by local image writes."""
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

os.environ.setdefault("MOYU_PPT_CONFIG_DIR", tempfile.mkdtemp(prefix="ppt-config-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ref_gallery as gallery


def pin(identity, ranks):
    return {"candidate_id": f"ref_{identity}", "image_url": f"https://i.pinimg.com/originals/{identity}.png",
            "pinterest_url": f"https://www.pinterest.com/pin/{identity}", "query_ranks": ranks, "source_link": ""}


def fake_download(url, out):
    width = 500 if "small" in url else 1000
    Image.new("RGB", (width, 600), "white").save(out)
    return {"local_path": out}


def cid(identity):
    return "ref_" + hashlib.sha256(f"https://i.pinimg.com/originals/{identity}.png".encode()).hexdigest()[:16]


def raw_pin(identity, query):
    """One pin as pinterest_search.normalize_pins returns it."""
    return {"id": identity, "query": query, "pinterest_url": f"https://www.pinterest.com/pin/{identity}",
            "image_url": f"https://i.pinimg.com/originals/{identity}.png", "source_link": ""}


BRIEF = {"style_intent": "像素风", "style_terms": [], "audience_use": "培训", "typography": "按需生成", "content_needs": ["目录"],
         "avoid": [], "exploration": "bounded", "scope": "directions",
         "decision": {"source": "user", "status": "confirmed", "evidence": "像素风格"}}


def query(text, group):
    return {"query": text, "direction": group, "purpose": "layout", "target": "演示页", "basis": "测试"}


class LazySearchTests(unittest.TestCase):
    """The gallery searches about as much as it shows: two keywords per group, more on request."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppt-ref-lazy-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = self.root / "plan.json"
        self.plan.write_text(json.dumps({"brief": BRIEF, "queries": [
            query("pixel slides", "题材"), query("pixel deck", "题材"), query("pixel keynote", "题材"),
            query("voxel slides", "体素"), query("voxel deck", "体素"), query("voxel keynote", "体素")]},
            ensure_ascii=False))
        self.results = {"pixel slides": [raw_pin(f"a{i}", "pixel slides") for i in range(4)],
                        "pixel deck": [raw_pin(f"b{i}", "pixel deck") for i in range(4)],
                        "pixel keynote": [raw_pin(f"c{i}", "pixel keynote") for i in range(18)],
                        "voxel slides": [raw_pin(f"v{i}", "voxel slides") for i in range(2)] + [raw_pin("a0", "voxel slides")],
                        "voxel deck": [raw_pin(f"w{i}", "voxel deck") for i in range(18)],
                        "voxel keynote": [raw_pin(f"k{i}", "voxel keynote") for i in range(18)]}
        self.calls = []
        self.out = self.root / "gallery"

    def search(self, text):
        self.calls.append(text)
        if text in getattr(self, "broken", ()):
            raise gallery.pinterest_search.PinterestSearchError("HTTP 500")
        return self.results[text]

    def build(self, **kwargs):
        plan = None if kwargs.get("more") else self.plan
        return gallery.build(None, plan, self.out, download=fake_download, search=self.search, **kwargs)

    def ids(self, state, group):
        return [item["candidate_id"] for item in state["items"] if item["group"] == group]

    def test_first_build_searches_two_keywords_per_group_and_alternates_between_them(self):
        state = self.build()
        self.assertEqual(sorted(self.calls), ["pixel deck", "pixel slides", "voxel deck", "voxel slides"])
        self.assertEqual(self.ids(state, "题材"), [cid(x) for x in ("a0", "b0", "a1", "b1", "a2", "b2")])
        # a0 is already on the page under 题材, so 体素 skips it.
        self.assertEqual(self.ids(state, "体素"), [cid(x) for x in ("v0", "w0", "v1", "w1", "w2", "w3")])
        self.assertEqual(state["search"]["searched"], 4)
        html = (self.out / "index.html").read_text()
        self.assertIn("pixel keynote（备用，多看时再搜）", html)
        self.assertIn("pixel deck（已搜）", html)
        self.assertIn("每组先搜前 2 条", html)

    def test_more_uses_what_was_found_before_searching_the_next_keyword(self):
        self.build()
        self.calls.clear()
        state = self.build(more=["题材"])  # 2 pins are left from the first two keywords; the third is searched
        self.assertEqual(self.calls, ["pixel keynote"])
        self.assertEqual(len(self.ids(state, "题材")), 12)
        self.calls.clear()
        state = self.build(more=["题材"])  # the third keyword still has plenty: no request
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.ids(state, "题材")), 18)
        self.assertEqual(len({item["candidate_id"] for item in state["items"]}), len(state["items"]))

    def test_the_user_can_ask_for_a_wider_search(self):
        state = self.build(keywords=3)
        self.assertEqual(len(self.calls), 6)
        self.assertIn("每组先搜前 3 条", (self.out / "index.html").read_text())
        self.assertEqual(state["keywords_first"], 3)

    def test_a_wider_search_of_one_group_does_not_change_the_others(self):
        self.build()
        self.calls.clear()
        state = self.build(more=["体素"], keywords=3)
        self.assertEqual(self.calls, ["voxel keynote"])
        self.assertEqual(state["keywords_first"], 2)
        self.calls.clear()
        self.build(more=["题材"])  # 题材 still searches its third keyword only because it runs short
        self.assertEqual(self.calls, ["pixel keynote"])

    def test_rebuild_reuses_the_saved_search(self):
        first = [item["candidate_id"] for item in self.build()["items"]]
        self.calls.clear()
        again = [item["candidate_id"] for item in self.build(rebuild=True)["items"]]
        self.assertEqual(self.calls, [])
        self.assertEqual(first, again)

    def test_a_failed_keyword_moves_on_to_the_next_one_and_is_reported(self):
        self.broken = {"pixel slides"}
        state = self.build()
        self.assertEqual(self.calls.count("pixel keynote"), 1)  # deck alone has 4: the third fills the group
        self.assertEqual(len(self.ids(state, "题材")), 6)
        self.assertIn("pixel slides", state["search"]["errors"])

    def test_the_search_needs_a_confirmed_brief(self):
        self.plan.write_text(json.dumps({"queries": [query("pixel slides", "题材")]}))
        with self.assertRaisesRegex(ValueError, "brief"):
            self.build()
        self.assertEqual(self.calls, [])


class GalleryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppt-ref-gallery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        pins = [pin(f"a{i}", {"pixel slides": i}) for i in range(10)]
        pins += [pin(f"b{i}", {"pixel deck": i}) for i in range(10)]
        pins += [pin(f"v{i}", {"voxel slides": i}) for i in range(4)]
        pins.append(pin("small", {"voxel slides": 4}))
        pins.append(pin("shared", {"pixel slides": 0.5, "voxel slides": 5}))  # also a top 题材 result
        (self.root / "refs.json").write_text(json.dumps({"pins": pins}))
        plan = {"queries": [{"query": "pixel slides", "direction": "题材"}, {"query": "pixel deck", "direction": "题材"},
                            {"query": "voxel slides", "direction": "体素"}]}
        (self.root / "plan.json").write_text(json.dumps(plan, ensure_ascii=False))
        self.out = self.root / "gallery"
        self.library = self.root / "library"

    def build(self, **kwargs):
        return gallery.build(self.root / "refs.json", self.root / "plan.json", self.out, download=fake_download, **kwargs)

    def test_groups_take_keywords_in_turn_in_pinterest_order(self):
        state = self.build()
        first = [item["candidate_id"] for item in state["items"] if item["group"] == "题材"]
        self.assertEqual(first, ["ref_a0", "ref_b0", "ref_shared", "ref_b1", "ref_a1", "ref_b2"])
        voxel = [item["candidate_id"] for item in state["items"] if item["group"] == "体素"]
        self.assertEqual(voxel, ["ref_v0", "ref_v1", "ref_v2", "ref_v3", "ref_small"])  # shared already shown above
        self.assertEqual([item["number"] for item in state["items"]], list(range(1, 12)))
        self.assertTrue((self.out / "index.html").is_file())
        self.assertEqual(len(state["boards"]), 2)
        html = (self.out / "index.html").read_text()
        self.assertIn('<span class="num">11</span>', html)
        self.assertIn("（偏小）", html)  # the uploader's size is shown, not used to drop the image
        self.assertEqual(gallery.summary(state)["small"], [11])  # ref_small is the 11th

    def test_more_continues_numbering_without_repeats_and_a_second_build_needs_a_flag(self):
        self.build()
        with self.assertRaisesRegex(ValueError, "--more"):
            self.build()
        state = gallery.build(None, None, self.out, more=["题材"], download=fake_download)
        more = [item for item in state["items"] if item["number"] > 11]
        self.assertEqual([item["number"] for item in more], list(range(12, 18)))
        self.assertEqual(len({item["candidate_id"] for item in state["items"]}), len(state["items"]))
        with self.assertRaisesRegex(ValueError, "No such group"):
            gallery.build(None, None, self.out, more=["没有这组"], download=fake_download)

    def test_rebuild_shows_the_same_images_again(self):
        first = [item["candidate_id"] for item in self.build()["items"]]
        again = [item["candidate_id"] for item in self.build(rebuild=True)["items"]]
        self.assertEqual(first, again)  # nothing is hidden because it was shown before

    def test_pick_records_use_and_saves_others_to_the_library(self):
        self.build()
        result = gallery.pick(self.out, use=[3], save=[1, 7], tags=["像素风"], note="3 这次用，1 和 7 存起来",
                              deck="test-deck", library=self.library)
        selected = json.loads((self.out / "selected-refs.json").read_text())
        self.assertEqual(selected["selected"][0]["candidate_id"], "ref_shared")
        self.assertTrue(Path(selected["selected"][0]["local_path"]).is_file())
        self.assertEqual(result["saved"], ["ref_a0", "ref_v0"])  # numbers 1 and 7
        index = json.loads((self.library / "index.json").read_text())
        self.assertEqual(len(index["items"]), 2)
        self.assertTrue((self.library / index["items"][0]["file"]).is_file())
        gallery.pick(self.out, save=[1], tags=["体素"], note="再存一次", library=self.library)
        entry = next(e for e in json.loads((self.library / "index.json").read_text())["items"] if e["id"] == "ref_a0")
        self.assertEqual(entry["tags"], ["像素风", "体素"])
        self.assertEqual(len(entry["notes"]), 2)
        with self.assertRaisesRegex(ValueError, "Not in this gallery"):
            gallery.pick(self.out, use=[99])
        with self.assertRaises(ValueError):
            gallery.pick(self.out)

    def test_library_is_searched_and_shown_before_any_search(self):
        self.build()
        gallery.pick(self.out, save=[1, 2], tags=["像素风"], library=self.library)
        gallery.pick(self.out, save=[7], tags=["体素"], library=self.library)
        self.assertEqual(len(gallery.library_matches(self.library, "像素风")), 2)
        self.assertEqual(len(gallery.library_matches(self.library)), 3)
        state = gallery.library_gallery(self.library, self.root / "lib-gallery", "像素风")
        self.assertEqual([item["number"] for item in state["items"]], [1, 2])
        result = gallery.pick(self.root / "lib-gallery", use=[2])
        self.assertEqual(result["use"][0]["candidate_id"], "ref_b0")
        with self.assertRaisesRegex(ValueError, "No saved reference"):
            gallery.library_gallery(self.library, self.root / "none", "水彩")

    def test_cli_reads_numbers_with_chinese_separators(self):
        self.assertEqual(gallery.parse_numbers("3，17、22 5"), [3, 17, 22, 5])
        with self.assertRaises(ValueError):
            gallery.parse_numbers("3,第五张")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = gallery.main(["library", "list", "--library", str(self.library)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["count"], 0)


class CategoryTests(unittest.TestCase):
    """Slide layouts and pictures of the style are searched in separate groups, shown under
    their category, and combined after the user picks from each."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppt-ref-category-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = self.root / "plan.json"
        element = {"direction": "风格元素·界面", "purpose": "image-language", "target": "界面与图标", "basis": "测试"}
        self.plan.write_text(json.dumps({"brief": BRIEF, "queries": [
            query("process slides", "版式·流程"), query("comparison deck", "版式·流程"),
            {**element, "query": "pixel game interface"}, {**element, "query": "pixel icons"}]}, ensure_ascii=False))
        self.results = {"process slides": [raw_pin(f"p{i}", "process slides") for i in range(4)],
                        "comparison deck": [raw_pin(f"c{i}", "comparison deck") for i in range(4)],
                        "pixel game interface": [raw_pin(f"u{i}", "pixel game interface") for i in range(4)],
                        "pixel icons": [raw_pin("small-icons", "pixel icons")] + [raw_pin(f"i{i}", "pixel icons") for i in range(3)]}
        self.out = self.root / "gallery"

    def build(self):
        return gallery.build(None, self.plan, self.out, download=fake_download, search=lambda text: self.results[text])

    def test_groups_and_images_carry_their_category(self):
        state = self.build()
        self.assertEqual([(g["group"], g["role"]) for g in state["groups"]], [("版式·流程", "layout"), ("风格元素·界面", "element")])
        self.assertEqual({item["role"] for item in state["items"] if item["group"] == "风格元素·界面"}, {"element"})
        page = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn("【版式】版式·流程", page)
        self.assertIn("【风格元素】风格元素·界面", page)
        self.assertIn("只看排版", page)
        self.assertIn("两边各挑一两张，之后把版式和画风搭配起来", state["lead"])
        self.assertEqual([g["role"] for g in gallery.summary(state)["groups"]], ["版式", "风格元素"])

    def test_every_image_is_kept_full_size_under_its_number_and_boards_do_not_enlarge(self):
        state = self.build()
        for item in state["items"]:
            large = self.out / item["large"]
            self.assertEqual(large.name.split(".")[0], f"{item['number']:02d}")
            with Image.open(large) as full, Image.open(self.out / item["file"]) as original:
                self.assertEqual(full.size, original.size)
        self.assertEqual(gallery.summary(state)["large"], str(self.out / "large"))
        with Image.open(self.out / state["boards"][0]) as board:
            self.assertEqual(board.width, 2 * (gallery.BOARD_TILE_WIDTH + 16) + 16)
        small = next(item for item in state["items"] if "small" in item["image_url"])
        with Image.open(self.out / state["boards"][1]) as board:  # a 500px original is pasted at 500px, not stretched
            row, column = divmod([i["number"] for i in state["items"] if i["group"] == small["group"]].index(small["number"]), 2)
            x = 16 + column * (gallery.BOARD_TILE_WIDTH + 16)
            y = 16 + row * (600 + 16)
            self.assertGreaterEqual(min(board.getpixel((x + 480, y + 300))), 250)   # the white image
            self.assertLessEqual(max(board.getpixel((x + 540, y + 300))), 247)      # the board behind it

    def test_pick_records_categories_and_reminds_when_one_is_missing(self):
        state = self.build()
        layout = next(item["number"] for item in state["items"] if item["role"] == "layout")
        element = next(item["number"] for item in state["items"] if item["role"] == "element")
        both = gallery.pick(self.out, use=[layout, element], note="版式用这张，画风用那张")
        self.assertEqual([entry["role"] for entry in both["use"]], ["版式", "风格元素"])
        self.assertNotIn("reminder", both)
        selected = json.loads((self.out / "selected-refs.json").read_text(encoding="utf-8"))["selected"]
        self.assertEqual([entry["role"] for entry in selected], ["layout", "element"])
        only_style = gallery.pick(self.out, use=[element], note="就这张")
        self.assertIn("没有版式参考", only_style["reminder"])
        only_layout = gallery.pick(self.out, use=[layout], note="就这张")
        self.assertIn("没有风格元素参考", only_layout["reminder"])

    def test_suggested_pairings_become_one_board_each_from_the_original_images(self):
        state = self.build()
        layout = next(item["number"] for item in state["items"] if item["role"] == "layout")
        element = next(item["number"] for item in state["items"] if item["role"] == "element")
        other = next(item["number"] for item in state["items"] if item["role"] == "element" and item["number"] != element)
        result = gallery.combo(self.out, [("第一套", [layout, element]), ("第二套", [layout, other, element])])
        first, second = result["suggestions"]
        self.assertEqual((first["layouts"], first["elements"]), ([layout], [element]))
        self.assertEqual(first["originals"], [f"large/{layout:02d}.png", f"large/{element:02d}.png"])  # the files to read
        with Image.open(self.out / first["board"]) as board:
            self.assertEqual(board.width, 2600)
            self.assertGreater(board.height, 600)  # the 1000x600 original is pasted at full size
        self.assertTrue((self.out / second["board"]).is_file())
        saved = json.loads((self.out / "suggestions.json").read_text(encoding="utf-8"))["suggestions"]
        self.assertEqual([item["name"] for item in saved], ["第一套", "第二套"])
        # "按第一套": the pairing's numbers are recorded as the user's picks.
        picked = gallery.pick(self.out, suggestion=1, note="按第一套")
        self.assertEqual([entry["number"] for entry in picked["use"]], [layout, element])
        with self.assertRaisesRegex(ValueError, "No suggestion 9"):
            gallery.pick(self.out, suggestion=9)
        with self.assertRaisesRegex(ValueError, "Not in this gallery"):
            gallery.combo(self.out, [("第三套", [999])])
        with self.assertRaisesRegex(ValueError, "at least one --set"):
            gallery.combo(self.out, [])


if __name__ == "__main__":
    unittest.main()
