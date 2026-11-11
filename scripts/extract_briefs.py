#!/usr/bin/env python3
"""Validate a whole confirmed deck, then mechanically emit briefs and job arrays.

All relative paths, including style-config assets, resolve against the deck cwd.
Validation failures never change existing outputs. --allow-partial permits gaps
in page numbers; every declared family anchor must still exist in the input.
deck-state.json must hold accepted intake and visual decisions, a reviewed font
plan and its artifacts. The approved style_config is used unless the identical
path is supplied explicitly. Full extraction must match the confirmed page count.

Lines tagged `锁=组名` get a 字框 computed from their character count (see
text_lock.py); a locked line that cannot fit at the locked size fails the split.
When a content skeleton is registered, every page must declare its [内容来源]
and the plan must align with the skeleton (see content_map.py); coverage.md is
written for the user to review. Decks that keep image/text budgets
(workflow.visual_budget_rules) need a [融合比] on every page within the confirmed
ratio and floor (see visual_budget.py); the page budget is added to its brief.
Decks with workflow.continuity_rules need a three-part [承接与递进] on every page,
and 变奏/亮点 pages a **构图方式** of their own (see continuity.py).
Decks that keep a copy sheet (page_copy.py) show exactly the confirmed lines:
a page whose 可见文字清单 differs from page-copy.md fails the split.
An optional **[编辑底图]**：[target] "path with spaces.png" declares a static
template edit. Its image must decode before any output is written; it replaces
automatic family/style references but keeps explicitly requested page assets.
"""
from __future__ import annotations
import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from deck_runtime import (TYPOGRAPHY_ROLES, atomic_write, json_bytes,
                          normalize_references, reject_positional_references,
                          validate_image_file)
from workflow_gate import require_workflow
import content_map
import page_copy
import continuity
import text_lock
import visual_budget

PAGE_RE = re.compile(r"^#{2,4}\s*第\s*(\d+)\s*页设计方案", re.M)
BRIEF_MARK_RE = re.compile(r"\*\*\[空间定位图\]\*\*")
FAMILY_RE = re.compile(r"\*\*\[场景族\]\*\*\s*[:：]\s*([^（(\n]+)[（(]([^）)]*)[）)]")
ANCHOR_PAGE_RE = re.compile(r"锚定页\s*P?(\d+)", re.I)
CONSTANTS_HEAD_RE = re.compile(r"^#{2,4}\s*全篇常量块.*$", re.M)
CLOSING_RE = re.compile(r"^>\s*全部", re.M)
PLACEHOLDER_LINE_RE = re.compile(r"^\[直接从构图开始[^\]]*\]\s*$", re.M)
# The copy list separates its fields with ；. Without this note a model may draw that ； at the
# end of a line of body text (seen on real pilot pages).
COPY_READING_NOTE = ("\n\n可见文字清单的读法：每一行“可见文字=”后面、第一个分号之前的才是要画的字，逐字照抄；"
                     "分号本身和它后面的“位置=”“角色=”“锁=”是给排版看的说明，不画进画面。"
                     "不要在任何一行字的末尾添加分号、句号或其他清单里没有的标点。")
# 自由构图: no size lock, no measurement; one plain sentence keeps content titles in the same family.
TITLE_FREE_NOTE = ("\n标题的样子（说明，不是画面文字）：这一页的标题和全篇其他内容页的标题是同一种字形、同一种做法，"
                   "看起来差不多大；位置和排法按上面的构图。标题字少不放大，字多不缩小，一行放不下就换行。")
STABLE_REF_RE = re.compile(r"\[(SZ\d+|S\d+|S-board|L-[\w-]+|T-[\w-]+|C\d+|family|target)\]")

def extract_constants(text):
    head = CONSTANTS_HEAD_RE.search(text)
    if not head:
        return None
    rest = text[head.end():]
    next_heading = re.search(r"^#{1,4}\s", rest, re.M)
    return (rest[:next_heading.start()] if next_heading else rest).strip()

def split_pages(text):
    matches = list(PAGE_RE.finditer(text))
    return [(int(match.group(1)), text[match.start():matches[i + 1].start()
             if i + 1 < len(matches) else len(text)]) for i, match in enumerate(matches)]

def extract_brief(section):
    mark = BRIEF_MARK_RE.search(section)
    if not mark:
        return None
    brief = section[mark.end():]
    closing = CLOSING_RE.search(brief)
    if closing:
        brief = brief[:closing.start()]
    return PLACEHOLDER_LINE_RE.sub("", brief).strip()

def field_text(section, name):
    pattern = re.compile(r"\*\*\[" + re.escape(name) + r"\]\*\*\s*[:：]\s*(.*?)(?=\n\s*\*\*\[|\Z)", re.S)
    return "\n".join(pattern.findall(section.split("**[空间定位图]**", 1)[0]))

def parse_family(section):
    match = FAMILY_RE.search(section)
    if not match:
        return None, None, False
    family, note = match.group(1).strip(), match.group(2)
    if "本页为族锚定页" in note or "自锚定" in note:
        return family, None, True
    anchor = ANCHOR_PAGE_RE.search(note)
    return family, int(anchor.group(1)) if anchor else None, False

RHYTHMS = (("变奏", "variation"), ("亮点", "highlight"), ("稳定", "stable"))

def parse_rhythm(section, number):
    """Optional [节奏] field: 稳定 (default) reuses the family skeleton; 变奏/亮点 may recompose."""
    value = field_text(section, "节奏").strip()
    if not value:
        return "stable"
    for word, rhythm in RHYTHMS:
        if value.startswith(word):
            return rhythm
    raise ValueError(f"P{number}: [节奏] must start with 稳定, 变奏 or 亮点")

def parse_reference_path(tail):
    """Accept quoted paths, or an unquoted path separated from its annotation.

    Quoting is required when punctuation inside the path could be mistaken for
    an annotation delimiter. For ordinary spaces, prefer the complete existing
    path, then an unambiguous existing prefix before free-form annotation.
    """
    tail = tail.strip()
    if not tail:
        return ""
    if tail[0] in {'"', "'", "`"}:
        lexer = shlex.shlex(tail, posix=True, punctuation_chars=";；,，—")
        lexer.whitespace_split = True
        lexer.commenters = ""
        lexer.quotes += "`"
        try:
            return lexer.get_token() or ""
        except ValueError as exc:
            raise ValueError("Unclosed reference path quote; quote the complete path before its annotation") from exc
    if Path(tail).is_file():
        return tail
    candidate = re.split(r"[；;，—]|[ \t]+--?[ \t]+|[ \t]+#", tail, maxsplit=1)[0].strip()
    if not candidate or Path(candidate).is_file():
        return candidate
    existing = []
    for separator in re.finditer(r"[ \t]+", candidate):
        prefix = candidate[:separator.start()].strip()
        if Path(prefix).is_file():
            existing.append(prefix)
    if len(existing) == 1:
        return existing[0]
    if len(existing) > 1:
        raise ValueError("Ambiguous unquoted reference path; put the complete path in quotes")
    return candidate

def declared_references(section, name, role, prefix, defaults=None):
    text = field_text(section, name)
    if not text.strip():
        return []
    pattern = re.compile(r"\[(" + prefix + r")\]([^\[\n]*)")
    refs = []
    matches = list(pattern.finditer(text))
    if not matches:
        raise ValueError(f"[{name}] has no valid reference IDs")
    for match in matches:
        ref_id, tail = match.group(1), match.group(2).strip()
        candidate = parse_reference_path(tail)
        if role == "typography":
            typ_role = ref_id[2:]
            if typ_role not in TYPOGRAPHY_ROLES:
                raise ValueError(f"Unknown typography role: {ref_id}")
            path = candidate or (defaults or {}).get(typ_role)
        else:
            path = candidate
        if not isinstance(path, str) or not path:
            raise ValueError(f"[{ref_id}] needs an image path in the page or style config")
        item = {"path": path, "role": role, "id": ref_id}
        if role == "typography":
            item["typography_role"] = typ_role
        refs.append(item)
    return refs

def parse_page_assets(section):
    return [ref["path"] for ref in declared_references(section, "参考图", "brand", r"C\d+")]

def parse_layout_anchors(section):
    return [ref["path"] for ref in declared_references(section, "版式锚", "layout", r"L-[\w-]+")]

def parse_edit_target(section, number):
    """An optional explicit, static editing input, never a generated family reference."""
    metadata = section.split("**[空间定位图]**", 1)[0]
    declarations = re.findall(r"\*\*\[编辑底图\]\*\*", metadata)
    if not declarations:
        return None
    if len(declarations) != 1:
        raise ValueError(f"P{number}: declare [编辑底图] only once")
    value = field_text(section, "编辑底图").strip()
    if not value.startswith("[target]"):
        raise ValueError(f"P{number}: [编辑底图] requires [target] and one image path")
    try:
        refs = declared_references(section, "编辑底图", "target", "target")
        if len(refs) != 1:
            raise ValueError("exactly one [target] image is required")
        normalize_references(refs)
        validate_image_file(refs[0]["path"])
    except (ValueError, OSError) as exc:
        raise ValueError(f"P{number}: invalid [编辑底图]: {exc}") from exc
    return refs[0]["path"]

def weight(config, name, fallback):
    value = config.get(name, fallback)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ValueError(f"{name} must be a number from 0 to 100")
    return value

def title_lines(brief):
    """[(text, position, lock group or None)] for each 角色=title line of a page's copy list."""
    text_list = brief.split("可见文字清单", 1)[1] if "可见文字清单" in brief else ""
    found = []
    for line in text_list.splitlines():
        if not re.match(r"[ \t]*可见文字[ \t]*=", line):
            continue
        fields = {}
        for part in re.split(r"[；;]", line):
            key, sep, value = part.partition("=")
            if sep:
                fields.setdefault(key.strip(), value.strip())
        if fields.get("角色", "").lower() == "title":
            found.append((fields.get("可见文字", ""), fields.get("位置", ""), fields.get("锁") or None))
    return found


FREE_SAME_SPOT = 4  # this many content pages with one identical title position is a template again


def title_layout_check(layout, briefs, kinds):
    """(problems, warnings): does the plan keep the title layout the user chose at ①?

    uniform: content-page titles carry a 字号锁, so they sit at one position in one size.
    free: content-page titles carry none; each page places its title by its own composition."""
    content = {number: title_lines(brief) for number, brief in sorted(briefs.items()) if kinds.get(number) == "nodes"}
    content = {number: lines for number, lines in content.items() if lines}
    if not content:
        return [], []
    pages = lambda numbers: "、".join(f"P{number}" for number in numbers)
    locked = [number for number, lines in content.items() if any(lock for _, _, lock in lines)]
    problems, warnings = [], []
    if layout == "uniform":
        loose = [number for number in content if number not in locked]
        if not locked:
            problems.append("用户选的是统一版式，但内容页的标题都没有字号锁：在常量块声明“字号锁 标题”，"
                            "并在每页标题那一行标“锁=标题”。")
        elif loose:
            warnings.append(f"统一版式：{pages(loose)} 的标题没有字号锁，这几页的标题位置和大小不受约束。"
                            "有意做成特殊页就保留，否则补上“锁=”。")
    elif layout == "free":
        if locked:
            problems.append(f"用户选的是自由构图，{pages(locked)} 的标题却带着字号锁：去掉这几行的“锁=”，"
                            "标题的位置和排法写进这一页的构图方式。要每页同一位置、同一大小，请用户改选统一版式。")
        spots = {}
        for number, lines in content.items():
            spots.setdefault(lines[0][1], []).append(number)
        spot, users = max(spots.items(), key=lambda item: len(item[1]))
        if len(content) >= FREE_SAME_SPOT and len(users) == len(content):
            warnings.append(f"自由构图：{len(users)} 页内容页的标题位置写的都是“{spot}”，和统一版式没有区别。"
                            "按每页的内容安排标题：放进场景里的招牌或横幅、压在主体上、竖排、居中、放在底部都可以。")
    return problems, warnings


def build_outputs(args):
    """Pure preflight/build: return all output bytes, never mutate the filesystem."""
    text = Path(args.plans_file).read_text(encoding="utf-8")
    constants = extract_constants(text)
    if not constants:
        raise ValueError("A nonempty 全篇常量块 is required")
    reject_positional_references(constants)
    lock_groups = text_lock.parse_lock_groups(constants)
    # One stable [SZn] per size template, in declaration order; a template shared by two groups keeps one ID.
    template_ids = {}
    for spec in lock_groups.values():
        if spec.get("template") and spec["template"] not in template_ids:
            template_ids[spec["template"]] = f"SZ{len(template_ids) + 1}"
    aspect = text_lock.aspect_value(getattr(args, "aspect_ratio", None) or "16:9")
    # Drift measured on earlier images (text_lock.py check --feedback) is added to those pages' briefs.
    feedback_path = getattr(args, "text_lock_feedback", None)
    lock_feedback = text_lock.load_feedback(Path(args.out_dir) / feedback_path) if feedback_path else {}
    # (base picture share, floor) from the confirmed brief; None leaves page budgets unchecked.
    visual_limits = getattr(args, "visual_limits", None)
    config = json.loads(Path(args.style_config).read_text(encoding="utf-8")) if args.style_config else {}
    if not isinstance(config, dict):
        raise ValueError("Style config must be an object")
    schema_version = config.get("schemaVersion", 1 if args.style_config else 2)
    if isinstance(schema_version, bool) or schema_version not in (1, 2):
        raise ValueError("Supported style schemaVersion values are 1 and 2")
    style_weight = weight(config, "styleWeight", 90)
    layout_weight = weight(config, "layoutWeight", style_weight if schema_version == 1 else 90)
    layout_weight_source = ("explicit" if "layoutWeight" in config else
                            "legacy-styleWeight-fallback" if schema_version == 1 else "v2-default-90")
    family_references_enabled = style_weight >= 70
    typography = config.get("typography") or {}
    if not isinstance(typography, dict) or not isinstance(typography.get("references", {}), dict):
        raise ValueError("typography.references must be an object mapping roles to paths")
    type_defaults = typography.get("references", {})
    if set(type_defaults) - TYPOGRAPHY_ROLES:
        raise ValueError("typography.references contains unsupported roles")
    if any(not isinstance(path, str) or not path for path in type_defaults.values()):
        raise ValueError("Each typography role must select exactly one nonempty image path, not multiple candidates")
    type_policy = typography.get("referencePolicy") or {}
    if not isinstance(type_policy, dict):
        raise ValueError("typography.referencePolicy must be an object")
    type_enabled = type_policy.get("mode", "as-needed") != "off"
    style_paths = list(args.style_ref)
    if not style_paths and config.get("styleRefImage"):
        style_paths = [config["styleRefImage"]]
    style_refs = [{"path": path, "role": "style", "id": f"S{i + 1}"}
                  for i, path in enumerate(style_paths)] if style_weight >= 70 else []
    normalize_references(style_refs)
    board_path = config.get("styleBoardImage")
    if board_path is not None and (not isinstance(board_path, str) or not board_path.strip()):
        raise ValueError("styleBoardImage must be a nonempty image path or null")
    # The style board carries the deck's look without any slide layout to copy.
    # One image may serve as both the style reference and the board (a board that also shows
    # the recurring characters): it is uploaded once, as S, and rhythm pages follow it.
    board_is_style = bool(board_path) and any(
        Path(ref["path"]).resolve() == Path(board_path).resolve() for ref in style_refs)
    board_refs = normalize_references([{"path": board_path, "role": "style", "id": "S-board"}]) \
        if board_path and style_weight >= 70 and not board_is_style else []
    raw_pages = split_pages(text)
    if not raw_pages:
        raise ValueError("No 第X页设计方案 sections found")
    numbers = [num for num, _ in raw_pages]
    if any(num < 1 for num in numbers) or len(numbers) != len(set(numbers)):
        raise ValueError("Page numbers must be positive and unique")
    if not args.allow_partial and sorted(numbers) != list(range(1, max(numbers) + 1)):
        raise ValueError("Page numbers must form 1..N without gaps; use --allow-partial only for intentional subsets")
    pages = {}
    for number, section in raw_pages:
        brief = extract_brief(section)
        if not brief:
            raise ValueError(f"P{number}: nonempty [空间定位图] is required")
        text_list = brief.split("可见文字清单", 1)[1] if "可见文字清单" in brief else ""
        visible_lines = re.findall(r"^[ \t]*可见文字[ \t]*=[ \t]*([^；;\r\n]*)", text_list, re.M)
        if not visible_lines or not all(value.strip() for value in visible_lines):
            raise ValueError(f"P{number}: a nonempty 可见文字清单 with 可见文字= is required")
        reject_positional_references(brief)
        family, anchor, is_anchor = parse_family(section)
        if not family or (not is_anchor and anchor is None):
            raise ValueError(f"P{number}: a valid [场景族] and explicit anchor declaration are required")
        layouts = declared_references(section, "版式锚", "layout", r"L-[\w-]+")
        brands = declared_references(section, "参考图", "brand", r"C\d+")
        types = declared_references(section, "字体参考", "typography", r"T-[\w-]+", type_defaults) if type_enabled else []
        type_roles = [ref["typography_role"] for ref in types]
        if len(type_roles) != len(set(type_roles)):
            raise ValueError(f"P{number}: each typography role may declare only one reference")
        locks = text_lock.page_locks(brief, lock_groups, number, aspect)
        sizes = [{"path": path, "role": "size", "id": template_ids[path]}
                 for path in dict.fromkeys(lock["template"] for lock in locks if lock.get("template"))]
        brands = brands + sizes  # size templates travel with every page of the group, like brand assets
        normalize_references(layouts + types + brands)
        pages[number] = {"brief": brief, "family": family, "anchor": anchor, "text_locks": locks,
                         "rhythm": parse_rhythm(section, number),
                         "is_anchor": is_anchor, "layouts": layouts if layout_weight >= 70 else [],
                         "types": types, "brands": brands,
                         "target_image": parse_edit_target(section, number)}
    # Static templates must survive the whole batch, including another page's output.
    image_outputs = {str((Path(args.out_dir) / args.images_dir / f"p{number:02d}.png").resolve()): number
                     for number in pages}
    for number, page in pages.items():
        target = page["target_image"]
        if target and str(Path(target).resolve()) in image_outputs:
            collision = image_outputs[str(Path(target).resolve())]
            raise ValueError(f"P{number}: target_image must differ from every output; it is the output for P{collision}")
    for number, page in pages.items():
        visited = {number}
        current = page
        while not current["is_anchor"]:
            anchor = current["anchor"]
            if anchor not in pages:
                raise ValueError(f"P{number}: anchor P{anchor} does not exist in the input")
            if anchor in visited:
                raise ValueError(f"P{number}: cyclic anchor dependency through P{anchor}")
            visited.add(anchor)
            current = pages[anchor]
            if current["family"] != page["family"]:
                raise ValueError(f"P{number}: anchor P{anchor} belongs to a different family")
        if not page["is_anchor"] and not pages[page["anchor"]]["is_anchor"]:
            raise ValueError(f"P{number}: references must point directly to the declared family anchor")
    families = {}
    for number, page in pages.items():
        if page["is_anchor"]:
            if page["family"] in families:
                raise ValueError(f"Family '{page['family']}' declares multiple anchors")
            families[page["family"]] = number
    # Each page says how it joins its neighbours; 变奏/亮点 pages put their change into their own 构图方式.
    continuity_report, continuity_warnings = None, []
    if getattr(args, "continuity_rules", False):
        page_kinds = {number: page["kind"] for number, page in content_map.plan_pages(text).items()}
        problems, continuity_warnings, continuity_report = continuity.check_plan(
            raw_pages, {number: page["rhythm"] for number, page in pages.items()}, page_kinds)
        if problems:
            raise ValueError("前后页衔接没写清：\n" + "\n".join(f"  - {item}" for item in problems))
        # A lock fixes position as well as size: a divider that shares the content title's lock
        # gets its lettering pinned where every content title sits.
        content_groups = {lock["group"] for number, page in pages.items() if page_kinds.get(number) == "nodes"
                          for lock in page["text_locks"]}
        content_pages = [number for number in pages if page_kinds.get(number) == "nodes" and number != min(pages)]
        for group in sorted(content_groups):
            users = [number for number in content_pages if any(lock["group"] == group for lock in pages[number]["text_locks"])]
            # One shared position is what a user who chose the uniform layout asked for.
            if len(content_pages) >= 6 and len(users) == len(content_pages) and getattr(args, "title_layout", None) != "uniform":
                continuity_warnings.append(
                    f"{len(users)} 页内容页的标题都锁在同一个位置（字号锁“{group}”），每一页都从同一个角落开始。"
                    "想让版面有变化，给变奏页另设一个字高相同、位置不同的组（如右上、左下），标题一样大，位置换着用。")
        for number, page in sorted(pages.items()):
            name = continuity.DISPLAY_KINDS.get(page_kinds.get(number))
            shared = sorted({lock["group"] for lock in page["text_locks"]} & content_groups) if name else []
            if shared:
                continuity_warnings.append(
                    f"P{number} 是{name}，却套了内容页的字号锁“{'、'.join(shared)}”：它的大字会被固定在内容页标题的位置和大小。"
                    "展示页的大字单独构图，去掉这一行的锁；这几张确实要做成同一个版式的系列时，另设一个组。")
    # The title layout chosen at ①: uniform keeps content titles locked, free keeps them unlocked.
    title_layout, layout_warnings = getattr(args, "title_layout", None), []
    layout_kinds = {number: page["kind"] for number, page in content_map.plan_pages(text).items()} if title_layout else {}
    if title_layout:
        problems, layout_warnings = title_layout_check(
            title_layout, {number: page["brief"] for number, page in pages.items()}, layout_kinds)
        if problems:
            raise ValueError("标题排版和用户选的不一致：\n" + "\n".join(f"  - {item}" for item in problems))
    # 字数统一: the model draws short lines large and long ones small, so a group keeps one length.
    length_problems = text_lock.length_problems({number: page["text_locks"] for number, page in pages.items()}, lock_groups)
    if length_problems:
        raise ValueError("字号锁的字数不统一：\n" + "\n".join(f"  - {item}" for item in length_problems))
    files = {"deck-constants.txt": (text_lock.prompt_constants(constants) + "\n").encode("utf-8")}
    # A registered content skeleton makes every page answer for where its content comes from.
    content_report, content_warnings = None, []
    if getattr(args, "content_map", None):
        checked = content_map.check_map(args.content_map, getattr(args, "workflow_state", None))
        if not checked["ok"]:
            raise ValueError("内容骨架有问题：\n" + "\n".join(f"  - {item}" for item in checked["problems"]))
        alignment = content_map.check_plan(checked["skeleton"], text)
        if not alignment["ok"]:
            raise ValueError("方案和内容骨架不对位：\n" + "\n".join(f"  - {item}" for item in alignment["problems"]))
        files["coverage.md"] = content_map.coverage_table(checked["skeleton"], alignment).encode("utf-8")
        content_report = {"type": checked["type"], **alignment["summary"], "coverage_table": "coverage.md"}
        content_warnings = alignment["warnings"]
    # The copy sheet the user confirmed is the only text a page may show: no line more, no line less.
    if getattr(args, "page_copy", None):
        sheet, sheet_problems = page_copy.parse_copy(Path(args.page_copy).read_text(encoding="utf-8"))
        differences = sheet_problems or page_copy.compare_plan(sheet, text, partial=args.allow_partial)
        if differences:
            raise ValueError("方案里的可见文字和已确认的文案稿不一致。改方案；要改上屏的字，先改文案稿并请用户重新确认：\n"
                             + "\n".join(f"  - {item}" for item in differences))
    # Each page states its image/text budget; most pages keep the confirmed ratio, none go below the floor.
    budgets, kinds, budget_report, budget_warnings = {}, {}, None, []
    if visual_limits:
        kinds = {number: page["kind"] for number, page in content_map.plan_pages(text).items()}
        budgets = {number: visual_budget.page_budget(section) for number, section in raw_pages}
        problems, budget_warnings, budget_report = visual_budget.check_plan(
            {number: {"budget": budgets[number], "kind": kinds.get(number)} for number in budgets},
            *visual_limits, partial=args.allow_partial)
        if problems:
            raise ValueError("图文预算不合规：\n" + "\n".join(f"  - {item}" for item in problems))
    anchor_jobs, page_jobs, all_jobs, summary = [], [], [], []
    for number, page in sorted(pages.items()):
        prompt_relative = f"{args.briefs_dir}/p{number:02d}.txt"
        prompt_path = str(Path(args.out_dir) / prompt_relative)
        # Locked lines get a plain-language size instruction; text_lock.py measures and repairs the result.
        appendix = COPY_READING_NOTE + text_lock.brief_appendix(page["text_locks"], lock_feedback.get(number), template_ids)
        if title_layout == "free" and layout_kinds.get(number) == "nodes" and title_lines(page["brief"]):
            appendix += TITLE_FREE_NOTE
        # Lettering pages (cover, chapter, ending) carry no area budget: their big type is the picture.
        area_budget = budgets.get(number) if kinds.get(number) not in visual_budget.LETTERING else None
        appendix += visual_budget.brief_appendix(area_budget)
        files[prompt_relative] = (page["brief"] + appendix + "\n").encode("utf-8")
        extras = page["layouts"] + page["types"] + page["brands"]
        used_ids = set(STABLE_REF_RE.findall(page["brief"] + appendix))
        if page["target_image"]:
            # The target already carries style and layout. Keep explicitly requested S
            # references and this page's L/T/C/SZ assets, without any automatic family anchor.
            explicit_style = [ref for ref in style_refs + board_refs if ref["id"] in used_ids]
            refs = normalize_references(explicit_style + extras, target_image=page["target_image"])
            single_refs = refs
        elif page["is_anchor"]:
            refs = style_refs + extras
        elif family_references_enabled and page["rhythm"] != "stable" and (
                board_refs or board_is_style or (style_refs and args.style_ref_scope == "all")):
            # 变奏/亮点 pages follow a non-slide style carrier (the board, or an S image that
            # every page receives), not the anchor slide, so composition stays free.
            refs = board_refs + (style_refs if args.style_ref_scope == "all" or board_is_style else []) + extras
        elif family_references_enabled:
            refs = [{"path": str(Path(args.out_dir) / args.images_dir / f"p{page['anchor']:02d}.png"), "role": "family", "id": "family"}]
            refs += (style_refs if args.style_ref_scope == "all" else []) + extras
        else:
            refs = extras
        refs = normalize_references(refs, require_files=False)
        if not page["target_image"]:
            single_refs = normalize_references(
                (board_refs if page["rhythm"] != "stable" and not page["is_anchor"] else []) + style_refs + extras)
        effective_refs = single_refs if args.single_phase else refs
        missing_ids = used_ids - {ref["id"] for ref in effective_refs}
        if missing_ids:
            raise ValueError(
                f"P{number}: brief depends on disabled or unassigned reference IDs {sorted(missing_ids)}. "
                "Remove the dependent image-reference instructions and write the needed specification as text, "
                "or explicitly enable/assign those references. T mode=off never uploads T; styleWeight<70 "
                "never uploads S/S-board/family; [S-board] needs styleBoardImage and a 变奏/亮点 page "
                "or explicit use on an editing page; layoutWeight<70 never uploads L. "
                "[SZn] needs a size template assigned to this page's text lock; [target] needs [编辑底图]."
            )
        job = {"page": number, "family": page["family"],
               "stage": "anchor" if page["is_anchor"] else "page",
               "prompt_file": prompt_path, "output": str(Path(args.out_dir) / args.images_dir / f"p{number:02d}.png"),
               "references": refs}
        if page["target_image"]:
            job.update(edit=True, target_image=page["target_image"])
        if page["text_locks"]:
            job["text_locks"] = page["text_locks"]
        (anchor_jobs if page["is_anchor"] else page_jobs).append(job)
        all_jobs.append({**job, "stage": "single", "references": single_refs})
        summary.append({"page": number, "family": page["family"], "anchor": page["is_anchor"],
                        "rhythm": page["rhythm"], "references": refs})
    lock_usage = text_lock.group_usage({number: page["text_locks"] for number, page in pages.items()})
    files["manifest-anchors.json"] = json_bytes(anchor_jobs)
    files["manifest-pages.json"] = json_bytes(page_jobs)
    if args.single_phase:
        files["manifest-all.json"] = json_bytes(all_jobs)
    # Even oddly named image inputs must not be overwritten by an emitted brief/manifest.
    artifact_outputs = {str((Path(args.out_dir) / relative).resolve()) for relative in files}
    artifact_outputs.update(str(path.resolve()) for path in (Path(args.out_dir) / args.briefs_dir).glob("p[0-9]*.txt"))
    if not args.single_phase:
        artifact_outputs.add(str((Path(args.out_dir) / "manifest-all.json").resolve()))
    for number, page in pages.items():
        if page["target_image"] and str(Path(page["target_image"]).resolve()) in artifact_outputs:
            raise ValueError(f"P{number}: target_image would be overwritten or removed by extraction")
    report = {"constants": True, "pages": len(pages), "anchor_jobs": len(anchor_jobs),
              "page_jobs": len(page_jobs), "styleWeight": style_weight, "layoutWeight": layout_weight,
              "schemaVersion": schema_version, "layoutWeightSource": layout_weight_source,
              "familyReferencesEnabled": family_references_enabled and not args.single_phase,
              "style_refs": style_refs, "style_board": bool(board_refs),
              "rhythm": {name: sum(1 for item in summary if item["rhythm"] == name)
                         for name in ("stable", "variation", "highlight")}, "single_phase_manifest": args.single_phase,
              "allow_partial": args.allow_partial, "detail": summary, "text_locks": lock_usage,
              "title_layout": title_layout,
              "content": content_report, "visual_budget": budget_report, "continuity": continuity_report,
              "warnings": (["Legacy config: absent layoutWeight inherits styleWeight; set schemaVersion=2 for independent default 90"]
                           if layout_weight_source == "legacy-styleWeight-fallback" else [])
              + text_lock.usage_warnings(lock_usage, lock_groups) + content_warnings + budget_warnings
              + continuity_warnings + layout_warnings}
    return files, report

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plans_file")
    parser.add_argument("--out-dir", default=".")
    parser.add_argument("--briefs-dir", default="briefs")
    parser.add_argument("--images-dir", default="images")
    parser.add_argument("--style-ref", action="append", default=[])
    parser.add_argument("--style-config", help="Style JSON; also resolves per-page [T-role] references")
    parser.add_argument("--style-ref-scope", choices=["anchors", "all"], default="anchors")
    parser.add_argument("--single-phase", action="store_true", help="Also emit manifest-all.json without family-image dependencies; S is optional")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--text-lock-feedback", default="text-lock-feedback.json",
                        help="Drift notes from text_lock.py check, relative to --out-dir; used when the file exists")
    parser.add_argument("--workflow-state", default="deck-state.json", help="Confirmed brief and visual decisions; required before writing deck jobs")
    args = parser.parse_args(argv)
    try:
        workflow = require_workflow(args.workflow_state, "plans")
        approved_style = Path(workflow["artifact_paths"]["style_config"])
        if args.style_config and Path(args.style_config).resolve() != approved_style:
            raise ValueError("--style-config must match workflow.artifacts.style_config; review and record a changed visual proposal first")
        args.style_config = str(approved_style)
        args.aspect_ratio = workflow["brief"]["aspect_ratio"]  # 字框 widths depend on the confirmed canvas
        args.content_map = workflow["artifact_paths"].get("content_map")
        args.page_copy = workflow["artifact_paths"].get("page_copy")
        args.continuity_rules = workflow.get("continuity_rules", False)
        args.title_layout = workflow["brief"].get("title_layout") if workflow.get("title_layout_rules") else None
        if workflow.get("visual_budget_rules"):
            args.visual_limits = visual_budget.base_and_floor(workflow["brief"])
        for name in (args.briefs_dir, args.images_dir):
            path = Path(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("briefs/images directories must be relative descendants of --out-dir")
        files, report = build_outputs(args)
        require_workflow(args.workflow_state, "plans", pages=[page["page"] for page in report["detail"]],
                         require_complete=not args.allow_partial)
    except (ValueError, OSError, TypeError) as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return 1
    out = Path(args.out_dir)
    for relative, data in files.items():
        atomic_write(out / relative, data)
    for old in (out / args.briefs_dir).glob("p[0-9]*.txt"):
        if str(old.relative_to(out)) not in files:
            old.unlink()
    if "manifest-all.json" not in files:
        (out / "manifest-all.json").unlink(missing_ok=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
