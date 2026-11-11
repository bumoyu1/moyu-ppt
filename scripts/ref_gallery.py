#!/usr/bin/env python3
"""Numbered reference gallery for the user to pick from, and the local reference library.

The agent writes keywords; the user judges. Keyword groups come in two categories, slide
layouts (how to lay a page out) and pictures of the style itself (how to draw it); the
gallery shows them apart and the user picks from each. This tool never ranks, filters or
groups images by taste: a gallery shows, per keyword group of the query plan, the next pins
in Pinterest's own order (taking each searched keyword's next pin in turn), downloads
the originals and numbers them. The user replies with numbers: one or two to use for
this deck, and any others worth keeping, which go into the local reference library so
a later deck can start from them instead of searching again.

The search follows what is shown. One Pinterest request returns about 18 pins, so build
searches the first two keywords of each group (--keywords; the six shown alternate
between them); the group's other keywords wait until the user asks to see more of that
group and the pins already found run out, or asks for a wider search (--keywords 3).
Every result is kept in gallery/search.json, so no keyword is searched twice for one
gallery (--rebuild reuses it).

    ref_gallery.py build --plan inspiration-plan.json --out gallery     # searches as it goes
    ref_gallery.py build --out gallery --more 整套演示          # six more from one group
    ref_gallery.py build --out gallery --more 整套演示 --keywords 3   # the user wants this group searched wider
    ref_gallery.py build --refs refs.json --plan plan.json --out gallery  # an earlier full search, no new requests
    ref_gallery.py combo --gallery gallery --set 方案一=3,5,33 --set 方案二=12,40   # one board per suggested pairing
    ref_gallery.py pick --gallery gallery --suggestion 1                           # the user took pairing 1 as it is
    ref_gallery.py pick --gallery gallery --use 3,17 --save 5,22 --tags 像素风,体素 --note "用户原话"
    ref_gallery.py library list --query 像素风
    ref_gallery.py library gallery --query 像素风 --out library-gallery

Output of build: gallery/index.html (open in a browser; images load from gallery/images),
gallery/boards/*.jpg (one numbered board per group, for showing inside a chat; images are
not enlarged), gallery/large/NN.ext (every image at its original size, by number; read these,
not the boards, when describing a style or attaching a reference) and
gallery/gallery.json (number -> candidate). Previously shown images are not excluded:
every deck is a fresh choice.

The library lives in ${XDG_DATA_HOME:-~/.local/share}/moyu-ppt/references
(--library overrides), next to the finished-style template library. Only images the user
chose to save go in; each keeps its source link, the keyword it came from, tags and the
user's words.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pinterest_search  # noqa: E402

PER_GROUP = 6
KEYWORDS_FIRST = 2           # keywords searched per group before anything is shown
SEARCH_FILE = "search.json"  # every keyword searched for this gallery and what it returned
TILE_WIDTH = 800             # gallery page: tiles are shown at least this wide
BOARD_TILE_WIDTH = 1280      # boards: wide enough to read the small text of a slide
BOARD_TILE_HEIGHT = 1900     # boards: tall collages keep their detail up to this height
ROLE_LABELS = {"layout": "版式", "element": "风格元素"}
ROLE_HINTS = {"layout": "只看排版：分栏、层级、图文怎么摆；画风不用管",
              "element": "只看画风和元素：界面、图标、场景、纹理；它们不必是幻灯片"}
SMALL_WIDTH = 700            # originals narrower than this are marked 偏小 (the uploader's size)


def default_library() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base).expanduser() / "moyu-ppt" / "references"


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_numbers(text):
    numbers = []
    for part in re.split(r"[,，、\s]+", text or ""):
        if not part:
            continue
        if not part.isdigit():
            raise ValueError(f"'{part}' is not a gallery number")
        numbers.append(int(part))
    return list(dict.fromkeys(numbers))


# ---------------------------------------------------------------- selection (no taste involved)

def plan_groups(plan):
    """[(group, [queries])] in plan order; a query's group is its `direction` field."""
    groups = {}
    for entry in plan.get("queries") or []:
        groups.setdefault(entry.get("direction") or "未分组", []).append(entry["query"])
    return list(groups.items())


def group_roles(plan):
    """{group: "layout" | "element"} from the keywords' purposes; groups without a purpose have no role."""
    roles = {}
    for entry in plan.get("queries") or []:
        purpose = (entry.get("purpose") or "").strip()
        if purpose:
            roles.setdefault(entry.get("direction") or "未分组",
                             "layout" if purpose in pinterest_search.LAYOUT_PURPOSES else "element")
    return roles


def next_pins(pins, queries, shown, count):
    """Up to `count` pins not yet shown: each keyword's next pin in Pinterest order, in turn."""
    ranked = {q: sorted((p for p in pins if q in (p.get("query_ranks") or {})), key=lambda p: p["query_ranks"][q])
              for q in queries}
    picked, taken, depth = [], set(shown), 0
    longest = max((len(items) for items in ranked.values()), default=0)
    while len(picked) < count and depth < longest:
        for query in queries:
            if len(picked) < count and depth < len(ranked[query]):
                pin = ranked[query][depth]
                if pin["candidate_id"] not in taken:
                    taken.add(pin["candidate_id"])
                    picked.append((pin, query))
        depth += 1
    found = {p["candidate_id"] for items in ranked.values() for p in items}
    return picked, found


class Searches:
    """The keywords searched for one gallery, one request each, saved as they come back.

    Results of every searched keyword are merged together (pinterest_search's mechanical
    de-duplication), so a candidate keeps one id across groups."""

    def __init__(self, path, plan_entries, search=None):
        self.path = Path(path)
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        self.results = data.get("results") or {}
        self.errors = data.get("errors") or {}
        self.plan_entries = plan_entries
        self.search = search
        self.failed = set()   # failed in this run; tried again next time
        self.requests = 0

    def _one(self, query):
        try:
            return query, self.search(query), None
        except (pinterest_search.PinterestSearchError, OSError) as exc:
            return query, None, exc

    def run(self, *queries):
        """Search these keywords (at once when several) and save what came back."""
        queries = [q for q in queries if q not in self.results]
        if not queries:
            return
        if self.search is None:
            self.search = api_search()
        with ThreadPoolExecutor(min(4, len(queries))) as pool:
            done = list(pool.map(self._one, queries))
        for query, pins, exc in done:
            self.requests += 1
            if exc is None:
                self.results[query] = pins
                self.errors.pop(query, None)
            else:
                self.failed.add(query)
                self.errors[query] = str(exc)[:300]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"schema_version": 1, "results": self.results, "errors": self.errors},
                                        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def pins(self):
        planned = {entry["query"] for entry in self.plan_entries}
        results = {q: pins for q, pins in self.results.items() if q in planned}
        return pinterest_search.merge_candidates(results, self.plan_entries, 10 ** 9)[0]

    def fill(self, queries, on_page, count):
        """Next pins for a group; search its next keyword only when the searched ones run out."""
        while True:
            searched = [q for q in queries if q in self.results]
            picked, found = next_pins(self.pins(), searched, on_page, count)
            waiting = [q for q in queries if q not in self.results and q not in self.failed]
            if len(picked) >= count or not waiting:
                return picked, found
            self.run(waiting[0])


def api_search():
    api_key = pinterest_search.load_api_key()
    if not api_key:
        raise ValueError("No ScrapeCreators API key: set it up as in references/setup.md "
                         "(configure.py set-key scrapecreators)")

    def run(query):
        payload = pinterest_search.search_pinterest(query, api_key, timeout=20.0, retries=1)
        return pinterest_search.normalize_pins(payload, query=query)
    return run


# ---------------------------------------------------------------- files

def fetch(item, image_dir, download=None):
    """Download the original once; record its pixel size."""
    from PIL import Image
    download = download or pinterest_search.download_original
    suffix = Path(item["image_url"].split("?")[0]).suffix.lower() or ".jpg"
    out = image_dir / f"{item['candidate_id']}{suffix}"
    try:
        if not (out.is_file() and out.stat().st_size):
            download(item["image_url"], str(out))
        with Image.open(out) as img:
            item["width"], item["height"] = img.size
        item["file"] = f"images/{out.name}"
    except Exception as exc:  # noqa: BLE001 - one failed image must not stop the gallery
        item["file"], item["error"] = None, str(exc)[:160]
    return item


def write_boards(out, state):
    """One numbered board per group, two tiles per row, for showing inside a chat. Images are
    never enlarged: a small original stays small instead of turning into a blur."""
    from PIL import Image, ImageDraw, ImageFont
    boards = []
    (out / "boards").mkdir(exist_ok=True)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 40)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()
    for index, group in enumerate(state["groups"], 1):
        items = [item for item in state["items"] if item["group"] == group["group"] and item.get("file")]
        if not items:
            continue
        tiles = []
        for item in items:
            with Image.open(out / item["file"]) as img:
                tile = img.convert("RGB")
            scale = min(1, BOARD_TILE_WIDTH / tile.width, BOARD_TILE_HEIGHT / tile.height)
            if scale < 1:
                tile = tile.resize((max(1, round(tile.width * scale)), max(1, round(tile.height * scale))), Image.LANCZOS)
            tiles.append((item["number"], tile))
        pad, cols = 16, 2
        rows = [tiles[i:i + cols] for i in range(0, len(tiles), cols)]
        heights = [max(t.height for _, t in row) for row in rows]
        board = Image.new("RGB", (cols * (BOARD_TILE_WIDTH + pad) + pad, sum(heights) + pad * (len(rows) + 1)), "#f4f2ee")
        draw = ImageDraw.Draw(board)
        y = pad
        for row, height in zip(rows, heights):
            x = pad
            for number, tile in row:
                board.paste(tile, (x, y))
                draw.rectangle([x, y, x + 86, y + 56], fill="#111111")
                draw.text((x + 12, y + 6), str(number), fill="white", font=font)
                x += BOARD_TILE_WIDTH + pad
            y += height + pad
        path = out / "boards" / f"{index:02d}.jpg"
        board.save(path, quality=93, subsampling=0)
        boards.append(str(path.relative_to(out)))
    return boards


def write_large(out, state):
    """large/NN.ext: every shown image at its original size under its gallery number, so one
    image can be opened full size from a chat."""
    folder = out / "large"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir()
    for item in state["items"]:
        if not item.get("file"):
            continue
        source = out / item["file"]
        target = folder / f"{item['number']:02d}{source.suffix}"
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
        item["large"] = f"large/{target.name}"
    return "large"


def write_html(out, state):
    title = state.get("title") or "参考图库"
    head = (f"<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{html.escape(title)}</title><style>"
            ":root{--bg:#f4f2ee;--fg:#1d2329;--muted:#66707a;--card:#fff;--line:#d9d4cc;--accent:#2f6f4f}"
            "@media (prefers-color-scheme:dark){:root{--bg:#16191c;--fg:#e8e6e1;--muted:#9aa3ab;--card:#20252a;--line:#333a40;--accent:#7fc39d}}"
            "body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,'PingFang SC',sans-serif}"
            "main{max-width:1720px;margin:0 auto;padding:24px 16px 80px}h1{font-size:24px;margin:0 0 6px}"
            ".lead,.meta{color:var(--muted)}.meta{font-size:13px;margin:0 0 14px}nav{display:flex;flex-wrap:wrap;gap:6px 14px;margin:0 0 28px;font-size:13px}"
            "a{color:var(--accent)}nav a{text-decoration:none}section{margin:0 0 44px}h2{font-size:18px;margin:0 0 2px}"
            f".grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,{TILE_WIDTH}px),1fr));gap:18px}}"
            "figure{margin:0;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}"
            ".frame{position:relative;max-height:1500px;overflow:auto;background:#fff}.frame img{display:block;width:100%;height:auto}"
            ".num{position:absolute;left:10px;top:10px;background:#111;color:#fff;font-weight:700;font-size:22px;padding:2px 12px;border-radius:6px}"
            "figcaption{padding:8px 12px;font-size:13px;color:var(--muted);display:flex;justify-content:space-between;gap:10px}"
            ".small{color:#b4561f}</style></head><body><main>")
    parts = [head, f"<h1>{html.escape(title)}</h1><p class=\"lead\">{html.escape(state.get('lead', ''))}</p><nav>"]
    parts += [f'<a href="#g{i}">{html.escape((ROLE_LABELS[g["role"]] + "·" if g.get("role") else "") + g["group"])}</a>'
              for i, g in enumerate(state["groups"])]
    parts.append("</nav>")
    for i, group in enumerate(state["groups"]):
        meta = []
        if group.get("queries"):
            searched = group.get("searched")
            words = group["queries"] if searched is None else [
                q + ("（已搜）" if q in searched else "（备用，多看时再搜）") for q in group["queries"]]
            meta.append("关键词：" + " / ".join(words))
        if group.get("found") is not None:
            left = group["found"] - len(group["shown"])
            meta.append(f"搜到 {group['found']} 张，已放 {len(group['shown'])} 张" + (f"，还有 {left} 张" if left > 0 else ""))
        role = group.get("role")
        if role:
            meta.insert(0, f"{ROLE_LABELS[role]}：{ROLE_HINTS[role]}")
        label = (f"【{ROLE_LABELS[role]}】" if role else "") + group["group"]
        parts.append(f'<section id="g{i}"><h2>{html.escape(label)}</h2>'
                     f'<p class="meta">{"<br>".join(html.escape(m) for m in meta)}</p><div class="grid">')
        for item in [item for item in state["items"] if item["group"] == group["group"]]:
            link = f'<a href="{html.escape(item["pinterest_url"])}" target="_blank">来源</a>' if item.get("pinterest_url") else ""
            if not item.get("file"):
                parts.append(f'<figure><div class="frame"><span class="num">{item["number"]}</span></div>'
                             f'<figcaption><span>下载失败</span>{link}</figcaption></figure>')
                continue
            small = '<span class="small">（偏小）</span>' if item.get("width", 0) < SMALL_WIDTH else ""
            origin = f' · 来自“{html.escape(item["query"])}”' if item.get("query") else ""
            parts.append(f'<figure><div class="frame"><span class="num">{item["number"]}</span>'
                         f'<a href="{item["file"]}" target="_blank"><img loading="lazy" src="{item["file"]}" alt="候选 {item["number"]}"></a></div>'
                         f'<figcaption><span>原图 {item.get("width")}×{item.get("height")}{small}{origin}</span>{link}</figcaption></figure>')
        parts.append("</div></section>")
    parts.append("</main></body></html>")
    (out / "index.html").write_text("\n".join(parts), encoding="utf-8")


def publish(out, state, download=None, workers=8):
    image_dir = out / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    pending = [item for item in state["items"] if not item.get("file") and item.get("image_url")]
    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(lambda item: fetch(item, image_dir, download), pending))
    for item in state["items"]:  # library items are copied in already; only their size is read
        if item.get("file") and "width" not in item:
            from PIL import Image
            with Image.open(out / item["file"]) as img:
                item["width"], item["height"] = img.size
    write_html(out, state)
    state["boards"] = write_boards(out, state)
    state["large"] = write_large(out, state)
    (out / "gallery.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return state


def summary(state):
    result = {"gallery": state["out"], "shown": len(state["items"]),
              "failed": [item["number"] for item in state["items"] if not item.get("file")],
              "small": [item["number"] for item in state["items"] if item.get("file") and item.get("width", 0) < SMALL_WIDTH],
              "groups": [{"group": g["group"], "shown": len(g["shown"]), "found": g.get("found"),
                          **({"searched": g["searched"]} if "searched" in g else {})} for g in state["groups"]],
              "html": str(Path(state["out"]) / "index.html"), "boards": state.get("boards", []),
              "large": str(Path(state["out"]) / "large")}
    roles = {g["group"]: g["role"] for g in state["groups"] if g.get("role")}
    if roles:
        for group in result["groups"]:
            if group["group"] in roles:
                group["role"] = ROLE_LABELS[roles[group["group"]]]
    if "search" in state:
        result["search"] = state["search"]
    return result


# ---------------------------------------------------------------- commands

def build(refs_path, plan_path, out, per_group=PER_GROUP, more=(), rebuild=False, download=None, search=None,
          keywords=None):
    """Without refs_path the gallery searches as it goes (see the module docstring)."""
    out = Path(out)
    state_path = out / "gallery.json"
    if state_path.is_file() and not (more or rebuild):
        raise ValueError(f"{state_path} exists; add --more <组名> to show more of a group, or --rebuild to start over")
    if more:
        if not state_path.is_file():
            raise ValueError("--more needs an existing gallery; build it first")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        refs_path, plan_path = state.get("refs"), state["plan"]
    elif rebuild and out.exists():
        shutil.rmtree(out / "boards", ignore_errors=True)
        state_path.unlink(missing_ok=True)
        state = None
    else:
        state = None
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    if refs_path:  # an earlier full search: nothing more is requested
        pins = json.loads(Path(refs_path).read_text(encoding="utf-8")).get("pins") or []
        searches = None
    else:  # same gate as a formal search: confirmed brief, every group covered
        entries, _ = pinterest_search.load_search_plan([], str(plan_path), require_brief=True)
        searches = Searches(out / SEARCH_FILE, entries, search)
    if state is None:
        state = {"schema_version": 2, "kind": "search", "plan": str(Path(plan_path).resolve()),
                 "per_group": per_group, "keywords_first": keywords or KEYWORDS_FIRST, "created_at": now(),
                 "title": "参考图库", "groups": [], "items": []}
        if refs_path:
            state["refs"] = str(Path(refs_path).resolve())
        targets = [name for name, _ in plan_groups(plan)]
    else:
        unknown = [name for name in more if name not in {g["group"] for g in state["groups"]}]
        if unknown:
            raise ValueError("No such group: " + "、".join(unknown))
        targets = list(more)
    roles = group_roles(plan)
    groups = {g["group"]: g for g in state["groups"]}
    number = max((item["number"] for item in state["items"]), default=0)
    if searches:  # each group shown gets its first keywords searched, all requests at once
        first = keywords or state.get("keywords_first") or KEYWORDS_FIRST  # --keywords with --more: this call only
        searches.run(*[q for name, queries in plan_groups(plan) if name in targets for q in queries[:first]])
    for name, queries in plan_groups(plan):
        if name not in targets:
            continue
        group = groups.get(name) or {"group": name, "queries": queries, "shown": []}
        if roles.get(name):
            group["role"] = roles[name]
        # Nothing repeats within one gallery: a pin already shown under another group is skipped here.
        on_page = {item["candidate_id"] for item in state["items"]}
        if searches:
            picked, found = searches.fill(queries, on_page, state["per_group"])
            group["searched"] = [q for q in queries if q in searches.results]
        else:
            picked, found = next_pins(pins, queries, on_page, state["per_group"])
        group["found"] = len(found)
        for pin, query in picked:
            number += 1
            group["shown"].append(pin["candidate_id"])
            state["items"].append({"number": number, "candidate_id": pin["candidate_id"], "group": name, "query": query,
                                   **({"role": roles[name]} if roles.get(name) else {}),
                                   "image_url": pin["image_url"], "pinterest_url": pin.get("pinterest_url"),
                                   "source_link": pin.get("source_link") or None})
        if name not in groups:
            state["groups"].append(group)
            groups[name] = group
    if searches is not None:
        if not state["items"] and searches.errors:
            raise ValueError("Pinterest search failed: " + json.dumps(searches.errors, ensure_ascii=False)[:600])
        state["search"] = {"keywords": len(plan.get("queries") or []), "searched": len(searches.results),
                           "requests_this_run": searches.requests, "errors": searches.errors}
    state["out"] = str(out)
    order = "轮流取组内" + ("各条" if searches is None else "已搜") + "关键词在 Pinterest 上排最前的图"
    found_text = (f"{len(plan.get('queries') or [])} 条关键词分 {len(state['groups'])} 组，搜到 {len(pins)} 张不重复的图。"
                  if searches is None else
                  f"关键词分 {len(state['groups'])} 组，每组先搜前 {state.get('keywords_first') or KEYWORDS_FIRST} 条，"
                  f"这次一共搜了 {len(searches.results)} 条。")
    more_text = ("哪一组想多看，说一声再放。" if searches is None else
                 "哪一组想多看，说一声再放：先放这组已经搜到的，不够再搜它的下一条关键词；想让某组多搜几条，也可以直接说。")
    state["lead"] = (found_text + f"每组先放 {state['per_group']} 张：{order}，没有按风格或好坏删图。"
                     + ("“版式”组只看排版，“风格元素”组只看画风和元素：两边各挑一两张，之后把版式和画风搭配起来。"
                        if {"layout", "element"} <= set(roles.values()) else "")
                     + "图上大号数字是编号，点图打开原图。回复这次要用的编号，以及想存进风格库的编号；" + more_text)
    return publish(out, state, download)


def load_library(library):
    index = Path(library) / "index.json"
    if not index.is_file():
        return {"schema_version": 1, "items": []}
    data = json.loads(index.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError(f"{index}: expected an object with items")
    return data


def save_library(library, data):
    library = Path(library)
    library.mkdir(parents=True, exist_ok=True)
    temporary = library / "index.json.tmp"
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, library / "index.json")


CJK_FONTS = ("/System/Library/Fonts/STHeiti Medium.ttc", "/System/Library/Fonts/PingFang.ttc",
             "/System/Library/Fonts/Hiragino Sans GB.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
             "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "C:/Windows/Fonts/msyh.ttc")
SUGGESTIONS_FILE = "suggestions.json"


def _label_font(size):
    """(font, can_draw_chinese): a system CJK face when there is one, else a plain face."""
    from PIL import ImageFont
    for path in CJK_FONTS:
        try:
            return ImageFont.truetype(path, size), True
        except OSError:
            continue
    try:
        return ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size), False
    except OSError:
        return ImageFont.load_default(), False


def combo(gallery, sets):
    """One board per suggested pairing, built from the original images (never from the group
    boards): layouts on the left, style elements on the right, each under its gallery number,
    so the user sees a pairing as a whole. sets: [(name, [numbers])]."""
    from PIL import Image, ImageDraw
    gallery = Path(gallery)
    state = json.loads((gallery / "gallery.json").read_text(encoding="utf-8"))
    by_number = {item["number"]: item for item in state["items"]}
    if not sets:
        raise ValueError("Give at least one --set 名称=编号,编号")
    folder = gallery / "combos"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir()
    title_font, chinese = _label_font(64)
    label_font, _ = _label_font(40)
    number_font, _ = _label_font(44)
    width, pad, header = 2600, 28, 150
    suggestions = []
    for index, (name, numbers) in enumerate(sets, 1):
        numbers = list(dict.fromkeys(numbers))
        unknown = [n for n in numbers if n not in by_number]
        if unknown:
            raise ValueError("Not in this gallery: " + "、".join(map(str, unknown)))
        missing = [n for n in numbers if not by_number[n].get("file")]
        if missing:
            raise ValueError("No image file for: " + "、".join(map(str, missing)))
        if not numbers:
            raise ValueError(f"Suggestion {index} has no numbers")
        elements = [n for n in numbers if by_number[n].get("role") == "element"]
        layouts = [n for n in numbers if n not in elements]
        columns = [(ROLE_LABELS["layout"] if chinese else "Layout", layouts), (ROLE_LABELS["element"] if chinese else "Style", elements)]
        columns = [column for column in columns if column[1]]
        share = [0.6, 0.4] if len(columns) == 2 else [1.0]
        placed, x, tallest = [], pad, 0
        for (label, members), part in zip(columns, share):
            column_width = round((width - pad * (len(columns) + 1)) * part)
            y = header + 64
            for number in members:
                with Image.open(gallery / by_number[number]["file"]) as img:
                    tile = img.convert("RGB")
                scale = min(1, column_width / tile.width, 1900 / tile.height)
                if scale < 1:
                    tile = tile.resize((max(1, round(tile.width * scale)), max(1, round(tile.height * scale))), Image.LANCZOS)
                placed.append((number, tile, x, y))
                y += tile.height + pad
            placed.append((label, None, x, header))
            tallest = max(tallest, y)
            x += column_width + pad
        board = Image.new("RGB", (width, tallest + pad), "#f4f2ee")
        draw = ImageDraw.Draw(board)
        draw.rectangle([0, 0, width, header - 30], fill="#111111")
        draw.text((pad, 22), f"{index}" + (f"  {name}" if chinese or name.isascii() else ""), fill="white", font=title_font)
        for mark, tile, left, top in placed:
            if tile is None:
                draw.text((left, top), str(mark), fill="#111111", font=label_font)
                continue
            board.paste(tile, (left, top))
            draw.rectangle([left, top, left + 96, top + 62], fill="#111111")
            draw.text((left + 12, top + 6), str(mark), fill="white", font=number_font)
        path = folder / f"{index:02d}.jpg"
        board.save(path, quality=93, subsampling=0)
        suggestions.append({"index": index, "name": name, "numbers": numbers, "layouts": layouts, "elements": elements,
                            "board": str(path.relative_to(gallery)),
                            "originals": [by_number[n].get("large") or by_number[n]["file"] for n in numbers]})
    (gallery / SUGGESTIONS_FILE).write_text(json.dumps({"schema_version": 1, "created_at": now(), "suggestions": suggestions},
                                                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"gallery": str(gallery), "suggestions": suggestions}


def pick(gallery, use=(), save=(), tags=(), note="", deck=None, library=None, suggestion=None):
    gallery = Path(gallery)
    state = json.loads((gallery / "gallery.json").read_text(encoding="utf-8"))
    if suggestion is not None:  # the user took one of the suggested pairings as it is
        offered = json.loads((gallery / SUGGESTIONS_FILE).read_text(encoding="utf-8"))["suggestions"] \
            if (gallery / SUGGESTIONS_FILE).is_file() else []
        chosen_set = next((item for item in offered if item["index"] == suggestion), None)
        if chosen_set is None:
            raise ValueError(f"No suggestion {suggestion} in this gallery; run combo first")
        use = list(dict.fromkeys([*chosen_set["numbers"], *use]))
    by_number = {item["number"]: item for item in state["items"]}
    unknown = [n for n in [*use, *save] if n not in by_number]
    if unknown:
        raise ValueError("Not in this gallery: " + "、".join(map(str, unknown)))
    missing = [n for n in [*use, *save] if not by_number[n].get("file")]
    if missing:
        raise ValueError("No image file for: " + "、".join(map(str, missing)))
    if not (use or save):
        raise ValueError("Give --use and/or --save numbers from the user's reply")
    result = {"gallery": str(gallery), "picked_at": now(), "user_words": note or None}
    if use:
        chosen = [{**by_number[n], "local_path": str((gallery / by_number[n]["file"]).resolve())} for n in use]
        (gallery / "selected-refs.json").write_text(json.dumps(
            {"schema_version": 1, "stage": "picked_by_user", "user_words": note or None, "picked_at": result["picked_at"],
             "selected": chosen}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result["use"] = [{"number": item["number"], "candidate_id": item["candidate_id"], "local_path": item["local_path"],
                          **({"role": ROLE_LABELS[item["role"]]} if item.get("role") else {})} for item in chosen]
        offered = {group.get("role") for group in state["groups"]} - {None}
        lacking = [ROLE_LABELS[role] for role in ("layout", "element")
                   if role in offered and len(offered) == 2 and role not in {item.get("role") for item in chosen}]
        if lacking:  # a reminder for the agent to pass on, never a block: the choice is the user's
            result["reminder"] = (f"这次挑的图里没有{lacking[0]}参考。问用户要不要从“{lacking[0]}”组再挑一张；"
                                  "用户说不用，就在方向里写明这部分由你原创，等样页验证。")
        result["selected_refs"] = str(gallery / "selected-refs.json")
    if save:
        library = Path(library or default_library())
        data = load_library(library)
        entries = {entry["id"]: entry for entry in data["items"]}
        (library / "images").mkdir(parents=True, exist_ok=True)
        saved = []
        for n in save:
            item = by_number[n]
            source = gallery / item["file"]
            ref_id = item["candidate_id"]
            target = library / "images" / f"{ref_id}{source.suffix}"
            if not target.exists():
                shutil.copy2(source, target)
            entry = entries.get(ref_id) or {"id": ref_id, "file": f"images/{target.name}", "saved_at": now(),
                                             "image_url": item.get("image_url"), "pinterest_url": item.get("pinterest_url"),
                                             "source_link": item.get("source_link"), "query": item.get("query"),
                                             "group": item.get("group"), "role": item.get("role"), "width": item.get("width"),
                                             "height": item.get("height"), "tags": [], "notes": [], "decks": []}
            entry["tags"] = list(dict.fromkeys([*entry.get("tags", []), *tags]))
            if note and note not in entry.get("notes", []):
                entry.setdefault("notes", []).append(note)
            if deck and deck not in entry.get("decks", []):
                entry.setdefault("decks", []).append(deck)
            entries[ref_id] = entry
            saved.append(ref_id)
        data["items"] = list(entries.values())
        save_library(library, data)
        result["saved"] = saved
        result["library"] = str(library)
    return result


def library_matches(library, query=None):
    data = load_library(library)
    words = [w for w in re.split(r"[\s,，、]+", (query or "").lower()) if w]
    matches = []
    for entry in data["items"]:
        text = " ".join([*entry.get("tags", []), *entry.get("notes", []), entry.get("query") or "",
                         entry.get("group") or ""]).lower()
        if all(word in text for word in words):
            matches.append(entry)
    return matches


def library_gallery(library, out, query=None):
    """A numbered gallery of saved references, so the user can pick from them before any search."""
    library, out = Path(library or default_library()), Path(out)
    matches = library_matches(library, query)
    if not matches:
        raise ValueError("No saved reference matches" + (f" '{query}'" if query else ""))
    (out / "images").mkdir(parents=True, exist_ok=True)
    state = {"schema_version": 1, "kind": "library", "library": str(library), "query": query, "created_at": now(),
             "title": "风格库", "per_group": None, "groups": [], "items": [], "out": str(out)}
    groups = {}
    for number, entry in enumerate(matches, 1):
        source = library / entry["file"]
        target = out / "images" / source.name
        if not target.exists():
            shutil.copy2(source, target)
        name = (entry.get("tags") or [entry.get("group") or "未分组"])[0]
        group = groups.setdefault(name, {"group": name, "shown": []})
        group["shown"].append(entry["id"])
        state["items"].append({"number": number, "candidate_id": entry["id"], "group": name, "query": entry.get("query"),
                               "file": f"images/{target.name}", "image_url": entry.get("image_url"),
                               "pinterest_url": entry.get("pinterest_url"), "source_link": entry.get("source_link")})
    state["groups"] = list(groups.values())
    state["lead"] = (f"风格库里有 {len(matches)} 张匹配" + (f"“{query}”" if query else "") + "的参考图（以前挑中并存下的）。"
                     "回复这次要用的编号；都不合适就说“去搜”。")
    return publish(out, state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("build", help="Numbered gallery from a search, grouped by keyword group")
    p.add_argument("--refs", help="refs.json of an earlier full search (pinterest_search.py); omit to search as the gallery needs")
    p.add_argument("--plan", help="inspiration-plan.json (keywords grouped by direction; the first of each group is searched first)")
    p.add_argument("--out", required=True)
    p.add_argument("--per-group", type=int, default=PER_GROUP)
    p.add_argument("--more", action="append", default=[], help="Show the next images of this group; repeatable")
    p.add_argument("--keywords", type=int, help=f"Keywords searched per group up front (default {KEYWORDS_FIRST}); "
                   "with --more, search that many of the group before showing")
    p.add_argument("--rebuild", action="store_true")
    p = sub.add_parser("combo", help="One board per suggested pairing, built from the original images")
    p.add_argument("--gallery", required=True)
    p.add_argument("--set", action="append", default=[], dest="sets", metavar="名称=3,5,33",
                   help="A suggested pairing: its name and the gallery numbers it combines; repeatable")
    p = sub.add_parser("pick", help="Record the numbers the user replied with")
    p.add_argument("--gallery", required=True)
    p.add_argument("--use", default="", help="Numbers to use for this deck, e.g. 3,17")
    p.add_argument("--suggestion", type=int, help="The user took suggested pairing N (from combo) as it is")
    p.add_argument("--save", default="", help="Numbers to keep in the reference library")
    p.add_argument("--tags", default="", help="Tags for saved images, e.g. 像素风,体素")
    p.add_argument("--note", default="", help="The user's own words about the choice")
    p.add_argument("--deck")
    p.add_argument("--library")
    p = sub.add_parser("library", help="Search or show the reference library")
    p.add_argument("action", choices=["list", "gallery"])
    p.add_argument("--query")
    p.add_argument("--out")
    p.add_argument("--library")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            if not args.more and not args.plan:
                raise ValueError("build needs --plan (or --more on an existing gallery)")
            if args.per_group < 1 or (args.keywords is not None and args.keywords < 1):
                raise ValueError("--per-group and --keywords must be at least 1")
            result = summary(build(args.refs, args.plan, args.out, args.per_group, args.more, args.rebuild,
                                   keywords=args.keywords))
        elif args.command == "combo":
            sets = []
            for entry in args.sets:
                name, sep, numbers = entry.partition("=")
                if not sep or not name.strip():
                    raise ValueError(f"--set is written 名称=编号,编号: {entry}")
                sets.append((name.strip(), parse_numbers(numbers)))
            result = combo(args.gallery, sets)
        elif args.command == "pick":
            tags = [t for t in re.split(r"[,，、]", args.tags) if t.strip()]
            result = pick(args.gallery, parse_numbers(args.use), parse_numbers(args.save), [t.strip() for t in tags],
                          args.note.strip(), args.deck, args.library, args.suggestion)
        elif args.action == "list":
            matches = library_matches(args.library or default_library(), args.query)
            result = {"library": str(args.library or default_library()), "count": len(matches),
                      "items": [{k: e.get(k) for k in ("id", "tags", "notes", "query", "file", "saved_at")} for e in matches]}
        else:
            if not args.out:
                raise ValueError("library gallery needs --out")
            result = summary(library_gallery(args.library, args.out, args.query))
    except (OSError, ValueError, KeyError) as exc:
        print(f"ref_gallery failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
