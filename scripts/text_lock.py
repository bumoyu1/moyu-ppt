#!/usr/bin/env python3
"""Text locks: one type size for same-role lines on every page.

In observed image-generation runs, a requested "font size" was not a hard
typesetting control. A model may scale a line toward its perceived text area:
short titles can grow and long ones can shrink; percentage-only heights were
not always followed.
In one measured deck, 7-character
titles came out 25% larger than 15-character ones under the same spec, and the
two longest titles stopped at 57.8% and 58.7% of the width against a 58% limit.

The plan's constants block therefore declares an explicit size-and-anchor group

    - 字号锁 标题：字高 6.5%，顶 7%，左 4.6%，最宽 58%

and tags the lines it governs in the copy list (`可见文字=…；锁=标题`). For
each locked line extract_briefs.py computes a tight 字框 from the character
count - one height for the whole group, width = count x character width - and
writes it into that page's brief. The computed width is a layout budget, not
a command to stretch lettering to fill its right edge. A line that cannot fit 最宽 at the locked
size fails the split (字数预算): shorten it or split it into title + subtitle.
The script rejects an overlong line rather than silently shrinking it.

Those observed runs also varied line size with length: short titles grew and
long ones shrank.
So lines of one group are written to the same length (字数统一): lines of six
characters or fewer (display lettering such as chapter titles) have exactly the
same count, longer lines (page titles) differ by at most 2. A group that really
needs other lengths says so on its line, `字数 7–9`; anything else fails the split.

A reference showing size appeared helpful in another observed deck:
pages attached to the family anchor kept titles within a few percent, while
pages seeing only a style image grew them by 16-37%. This is a local observation,
not a guarantee for other models or decks. After the pilot, `template`
cuts the locked lines out of the page the user picked onto a plain canvas of the
same size (the lock then takes that page's measured size); the lock line names it (`样板 assets/typography/size-title.png`) and every
page that uses the group gets it as a [SZ] size reference, composition left free.

    text_lock.py template --manifest manifest-anchors.json --manifest manifest-pages.json \
        --page 11 --group 标题 --out assets/typography/size-title.png

After generation `check` measures the ink box of every locked line. It is a
free local report for the user, never a trigger: which pages are redrawn is the
user's call after looking at them, so feedback is recorded only for the pages
they name, and the next split adds it to those briefs alone:

    text_lock.py check --manifest manifest-anchors.json --manifest manifest-pages.json --report text-lock-report.json
    text_lock.py check --manifest manifest-anchors.json --manifest manifest-pages.json \
        --feedback text-lock-feedback.json --pages 6,9     # the pages the user chose to redraw

This tool never edits a slide and never calls a provider; `template` writes a new
reference image from an approved one. `guide` instead renders native-font text
on a new full-size canvas, calibrated to an ink height and positioning anchor;
it carries geometry only, not the target slide's glyph style.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import unicodedata
from array import array
from collections import deque
from datetime import datetime
from pathlib import Path

GROUP_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*•][ \t]*)?字号锁[ \t]+([^\s：:；;，,]+)[ \t]*[：:][ \t]*(.+?)[ \t]*$", re.M)
PERCENT_PART_RE = re.compile(r"^(字高|顶|左|居中|右|最宽)[ \t]*([0-9]+(?:\.[0-9]+)?)[ \t]*%$")
RATIO_PART_RE = re.compile(r"^(字宽比)[ \t]*([0-9]+(?:\.[0-9]+)?)$")
COUNT_PART_RE = re.compile(r"^字数[ \t]*([0-9]+)(?:[ \t]*[-–~～至到][ \t]*([0-9]+))?(?:[ \t]*个?字)?$")
TEMPLATE_PART_RE = re.compile(r"^样板[ \t]*[:：]?[ \t]*(\S.*)$")
GROUP_KEYS = {"字高": "ink_height_pct", "顶": "top_pct", "左": "left_pct", "居中": "center_x_pct",
              "右": "right_pct", "最宽": "max_width_pct", "字宽比": "width_ratio"}
ANCHOR_KEYS = ("left_pct", "center_x_pct", "right_pct")
ITEM_LOCK_RE = re.compile(r"[；;][ \t]*锁[ \t]*=[ \t]*([^；;\r\n]+?)[ \t]*(?=[；;]|$)")
ITEM_POSITION_RE = re.compile(r"[；;][ \t]*位置[ \t]*=[ \t]*([^；;\r\n]*)")
MULTILINE_HINT_RE = re.compile(r"分两行|分三行|两行|三行|多行|换行")
COPY_LINE_RE = re.compile(r"^[ \t]*可见文字[ \t]*=[ \t]*([^；;\r\n]*)(.*)$", re.M)

# A full-width character advances about 1.05x the ink height of its line in a plain
# bold face (0.99-1.09 measured on real pages); outlined or extruded display lettering
# is nearer 0.85 because the ink box also holds the outline and depth.
DEFAULT_WIDTH_RATIO = 1.05
HALF_WIDTH_UNIT = 0.55       # Latin letters, digits and ASCII punctuation
SAFE_MARGIN_PCT = 5.0        # default 最宽 keeps this much canvas free on the far side
SHORT_LINE_UNITS = 6         # display lettering: lines this short share one exact count
LENGTH_SPREAD = 2            # longer locked lines of one group differ by at most 2 characters

TOLERANCE_HEIGHT = 0.04      # ink height may differ from the lock by 4%
# Size is what viewers compare when flipping pages. With the 字框 instruction alone the line still
# lands 1-2.6% of the canvas side away from its box (one 3-page comparison), so position is looser.
TOLERANCE_POSITION = 3.0     # anchor edge and top may move 3% of the canvas side
BACKGROUND_TOLERANCE = 28    # summed |dR|+|dG|+|dB| still counted as background
MIN_BACKGROUND_SHARE = 0.5   # the search-window border must be mostly one flat colour


# ---------------------------------------------------------------- plan syntax

def aspect_value(aspect_ratio):
    """Canvas width / height from '16:9' or '1536x864'."""
    try:
        w, h = (float(part) for part in re.split(r"[:xX]", str(aspect_ratio)))
    except ValueError as exc:
        raise ValueError(f"Invalid aspect ratio: {aspect_ratio}") from exc
    if w <= 0 or h <= 0:
        raise ValueError(f"Invalid aspect ratio: {aspect_ratio}")
    return w / h


def parse_lock_groups(constants):
    """Read `字号锁 组名：字高 X%，顶 Y%，左|居中|右 Z%[，最宽 W%][，字宽比 R][，字数 N|a–b][，样板 路径]` lines."""
    groups = {}
    for match in GROUP_LINE_RE.finditer(constants or ""):
        name, body = match.group(1), match.group(2).rstrip("。.；; ")
        spec = {}
        for part in filter(None, (p.strip() for p in re.split(r"[，,、]", body))):
            count, template = COUNT_PART_RE.match(part), TEMPLATE_PART_RE.match(part)
            if count or template:
                key = "chars_min" if count else "template"
                if key in spec:
                    raise ValueError(f"字号锁 {name}: {'字数' if count else '样板'} is declared twice")
                if count:
                    spec["chars_min"] = int(count.group(1))
                    spec["chars_max"] = int(count.group(2) or count.group(1))
                else:
                    spec["template"] = template.group(1).strip()
                continue
            found = PERCENT_PART_RE.match(part) or RATIO_PART_RE.match(part)
            if not found:
                raise ValueError(f"字号锁 {name}: cannot read '{part}'; use 字高/顶/左/居中/右/最宽 with a % value, "
                                 "字宽比 with a number, 字数 4 or 字数 7–9, or 样板 with an image path")
            key = GROUP_KEYS[found.group(1)]
            if key in spec:
                raise ValueError(f"字号锁 {name}: {found.group(1)} is declared twice")
            spec[key] = float(found.group(2))
        if name in groups:
            raise ValueError(f"字号锁 {name} is declared twice")
        groups[name] = validate_spec(spec, name)
    return groups


PLANNING_PART_RE = re.compile(r"[ \t]*[，,、][ \t]*(?:字数|样板)[^，,、\r\n]*")


def prompt_constants(constants):
    """Constants as the model reads them: 字数 and 样板 are planning fields, not drawing
    instructions, so they are dropped from lock lines (adding a 样板 later must not change
    every page's prompt)."""
    return GROUP_LINE_RE.sub(lambda m: PLANNING_PART_RE.sub("", m.group(0)), constants or "")


def validate_spec(spec, name="lock"):
    if not isinstance(spec, dict):
        raise ValueError(f"{name}: text lock spec must be an object")
    for key in ("ink_height_pct", "top_pct"):
        if key not in spec:
            raise ValueError(f"字号锁 {name}: 字高 and 顶 are required")
    if len([key for key in ANCHOR_KEYS if key in spec]) != 1:
        raise ValueError(f"字号锁 {name}: declare exactly one of 左, 居中 or 右")
    if "chars_min" in spec or "chars_max" in spec:
        low, high = spec.get("chars_min"), spec.get("chars_max")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (low, high)) or not 0 < low <= high:
            raise ValueError(f"字号锁 {name}: 字数 must be a count such as 4, or a range such as 7–9")
    if "template" in spec and (not isinstance(spec["template"], str) or not spec["template"].strip()):
        raise ValueError(f"字号锁 {name}: 样板 needs an image path")
    extras = {key: spec[key] for key in ("chars_min", "chars_max", "template") if key in spec}
    spec = {key: value for key, value in spec.items() if key not in extras}
    for key, value in spec.items():
        if key not in GROUP_KEYS.values() or isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"字号锁 {name}: invalid field {key}")
        if key == "width_ratio":
            if not 0.4 <= value <= 2:
                raise ValueError(f"字号锁 {name}: 字宽比 must be between 0.4 and 2")
        elif not 0 < value < 100:
            raise ValueError(f"字号锁 {name}: {key} must be between 0 and 100 percent")
    if spec["ink_height_pct"] > 50:
        raise ValueError(f"字号锁 {name}: 字高 above 50% is not a text line")
    if spec["top_pct"] + spec["ink_height_pct"] > 100:
        raise ValueError(f"字号锁 {name}: the line would leave the canvas")
    return {**{key: float(value) for key, value in spec.items()}, **extras}


def text_units(text):
    """Line length in full-width character units: 汉字 1, Latin/digits about 0.55."""
    units = 0.0
    for char in text:
        if char.isspace():
            units += 0.3
        elif unicodedata.east_asian_width(char) in ("W", "F"):
            units += 1.0
        else:
            units += HALF_WIDTH_UNIT
    return round(units, 2)


def lock_box(spec, text, aspect):
    """The 字框 of one line: fixed group height, width from its character count."""
    units = text_units(text)
    if units <= 0:
        raise ValueError("Locked copy must not be empty")
    char_width = spec["ink_height_pct"] / aspect * spec.get("width_ratio", DEFAULT_WIDTH_RATIO)
    width = units * char_width
    top = spec["top_pct"]
    if "left_pct" in spec:
        x0 = spec["left_pct"]
        limit = spec.get("max_width_pct", 100 - x0 - SAFE_MARGIN_PCT)
    elif "right_pct" in spec:
        x0 = spec["right_pct"] - width
        limit = spec.get("max_width_pct", spec["right_pct"] - SAFE_MARGIN_PCT)
    else:
        x0 = spec["center_x_pct"] - width / 2
        limit = spec.get("max_width_pct", 2 * min(spec["center_x_pct"], 100 - spec["center_x_pct"]) - 2 * SAFE_MARGIN_PCT)
    return {"units": units, "char_width_pct": round(char_width, 3), "limit_pct": round(limit, 3),
            "max_units": int(limit / char_width + 1e-6),
            "box_pct": [round(x0, 3), round(top, 3), round(x0 + width, 3), round(top + spec["ink_height_pct"], 3)]}


def page_locks(brief, groups, number=None, aspect=16 / 9):
    """text_locks for one page from its 可见文字清单; enforces the character budget."""
    label = f"P{number}: " if number is not None else ""
    text_list = brief.split("可见文字清单", 1)[1] if "可见文字清单" in brief else ""
    locks, used = [], set()
    for match in COPY_LINE_RE.finditer(text_list):
        copy, tail = match.group(1).strip(), match.group(2)
        lock = ITEM_LOCK_RE.search(tail)
        if not lock:
            continue
        group = lock.group(1).strip()
        if group not in groups:
            raise ValueError(f"{label}锁={group} is not declared as 字号锁 in the constants block")
        position = ITEM_POSITION_RE.search(tail)
        if MULTILINE_HINT_RE.search(position.group(1) if position else ""):
            raise ValueError(f"{label}locked copy '{copy}' must be one visual line; list each line separately")
        if group in used:
            raise ValueError(f"{label}字号锁 {group} fixes one position, so it can lock only one line per page")
        used.add(group)
        spec = groups[group]
        box = lock_box(spec, copy, aspect)
        width = box["box_pct"][2] - box["box_pct"][0]
        if width > box["limit_pct"] + 1e-6:
            raise ValueError(
                f"{label}'{copy}' is {box['units']:g} 个字宽 and needs {width:.1f}% of the canvas width at 字号锁 {group}, "
                f"but only {box['limit_pct']:g}% is available: shorten it to about {box['max_units']} 个字, "
                "or split it into 主标题 + 副标题. The type size is locked and is never shrunk to fit.")
        if box["box_pct"][0] < 0 or box["box_pct"][2] > 100:
            raise ValueError(f"{label}'{copy}' would leave the canvas at 字号锁 {group}")
        locks.append({"text": copy, "group": group, **spec, "units": box["units"],
                      "char_width_pct": box["char_width_pct"], "max_units": box["max_units"],
                      "box_pct": box["box_pct"]})
    return locks


def group_usage(pages_locks):
    """Per group: pages and the range of line lengths (page number -> locks)."""
    usage = {}
    for number, locks in sorted(pages_locks.items()):
        for lock in locks:
            entry = usage.setdefault(lock["group"], {"pages": [], "units": []})
            entry["pages"].append(number)
            entry["units"].append(lock["units"])
    return {group: {"pages": entry["pages"], "min_units": min(entry["units"]), "max_units": max(entry["units"])}
            for group, entry in usage.items()}


def usage_warnings(usage, declared):
    warnings = [f"字号锁 {name} is declared but no copy line uses 锁={name}" for name in declared if name not in usage]
    for group, entry in usage.items():
        if not (declared.get(group) or {}).get("template"):
            warnings.append(
                f"字号锁 {group} 还没有字号样板：样页确认后，用 text_lock.py template 从用户选定的那一页做一张，写进这一行"
                "（样板 路径），再拆分生产其余页；没有样板时，不附锚定页的变奏、亮点页最容易画大。")
    return warnings


def length_problems(pages_locks, declared):
    """字数统一: the model sizes a line by its length, so one group keeps one length.
    Lines of six characters or fewer share one exact count; longer ones differ by at most 2.
    A `字数` field on the lock line replaces the default with the range it states."""
    lines = {}
    for number, locks in sorted(pages_locks.items()):
        for lock in locks:
            lines.setdefault(lock["group"], []).append((number, lock["text"], lock["units"]))
    problems = []
    for group, items in lines.items():
        spec = declared.get(group) or {}
        units = [item[2] for item in items]
        if "chars_min" in spec:
            low, high = spec["chars_min"], spec["chars_max"]
            stated = f"{low}" if low == high else f"{low}–{high}"
            bad = [item for item in items if not low - 0.01 <= item[2] <= high + 0.01]
            if bad:
                problems.append(f"字号锁 {group} 写明字数 {stated}，这些行不符：" + "、".join(
                    f"P{number}“{text}”{count:g} 个字" for number, text, count in bad)
                    + "。改写到这个字数；长的拆成主标题 + 副标题。")
            continue
        allowed = 0 if max(units) <= SHORT_LINE_UNITS else LENGTH_SPREAD
        if max(units) - min(units) > allowed + 0.01:
            by_count = {}
            for number, text, count in items:  # shown as whole characters; the rule itself uses exact widths
                by_count.setdefault(round(count), []).append(f"P{number}")
            spread = "；".join(f"{count} 字：{'、'.join(pages)}" for count, pages in sorted(by_count.items()))
            rule = "六个字以内的大字，每页字数相同" if allowed == 0 else f"同组标题最多差 {LENGTH_SPREAD} 个字"
            problems.append(
                f"字号锁 {group}：同组字数要统一（{rule}）。现在从 {round(min(units))} 到 {round(max(units))} 个字（{spread}）。"
                "模型会把短的画大、长的画小，所以在写方案时就把字数定齐：长的拆成主标题 + 副标题，短的补成同样字数；"
                "确实需要不同长度，在字号锁那一行写明“字数 a–b”。")
    return problems


def _pct(value):
    return f"{round(value, 1):g}%"


def brief_appendix(locks, feedback=None, template_ids=None):
    """Plain-language 字框 instruction appended to a page brief."""
    if not locks:
        return ""
    lines = ["", "字号锁（尺寸指令，不是画面文字。百分比以整张画布为 100%，横向从左边算起，纵向从上边算起；"
                 "与上文的位置描述不一致时以这里为准）：",
             "每行保持规定的固定墨迹字高（含描边、厚度与投影）和定位锚点，整行一行写完，保留字形比例与自然字距。"
             "按字数估算的横向字框只用于布局预算，不是必须顶满的右边界；行宽随实际字形自然形成，"
             "不要为了填满横向字框或旁边留白而放大、压缩、拉宽文字或加大字距。"
             "这几行字背后和四周一圈保持同一种平整的底色，不放云、建筑、纹理或别的图形：字才清楚，出图后也才量得出字号。底色用这一页画面本来的颜色，不要为此给这行字另加底牌、色块或边框。"]
    for lock in locks:
        x0, y0, x1, y1 = lock["box_pct"]
        anchor = (f"左边 {_pct(lock['left_pct'])}" if "left_pct" in lock else
                  f"右边 {_pct(lock['right_pct'])}" if "right_pct" in lock else
                  f"横向中心 {_pct(lock['center_x_pct'])}")
        lines.append(f"- “{lock['text']}”（{lock['units']:g} 个字宽）：字框横向 {_pct(x0)}–{_pct(x1)}，纵向 {_pct(y0)}–{_pct(y1)}；"
                     f"固定墨迹字高 {_pct(lock['ink_height_pct'])}，顶边 {_pct(lock['top_pct'])}，{anchor}。"
                     f"仅作布局估算，一行约放 {round(100 / lock['char_width_pct'])} 个字，不据此拉伸或放大字。")
    ids = sorted({(template_ids or {}).get(lock.get("template")) for lock in locks} - {None})
    if ids:
        lines.append("字号样板 " + "、".join(f"[{ref_id}]" for ref_id in ids) + " 与画布同比例，标出了这几行字应有的字高和位置："
                     "这一页的同一行字画成和样板里一样高、放在同样的位置。只照它的大小和位置，样板里的字不要照抄，"
                     "背景和构图也不要照搬。几何样板的原生字体可能与目标字体不同，不从样板迁移字形、字距或字宽；"
                     "实际字形沿用目标页或指定字形参考，行宽自然形成。")
    notes = [note for note in (feedback or []) if isinstance(note, dict)
             and any(note.get("text") == lock["text"] and note.get("group") == lock["group"] for lock in locks)]
    if notes:
        lines.append("上一次生成的偏差（只用于纠正，不是画面文字）：")
        lines.extend(f"- {note['note']}" for note in notes if isinstance(note.get("note"), str))
    return "\n".join(lines)


# ---------------------------------------------------------------- measurement

def _background(image, box):
    """Median colour of the box border and the share of border pixels near it."""
    x0, y0, x1, y1 = box
    px = image.load()
    border = [px[x, y0] for x in range(x0, x1)] + [px[x, y1 - 1] for x in range(x0, x1)]
    border += [px[x0, y] for y in range(y0, y1)] + [px[x1 - 1, y] for y in range(y0, y1)]
    middle = len(border) // 2
    bg = tuple(sorted(p[k] for p in border)[middle] for k in range(3))
    near = sum(1 for p in border if abs(p[0] - bg[0]) + abs(p[1] - bg[1]) + abs(p[2] - bg[2]) <= BACKGROUND_TOLERANCE)
    return bg, near / len(border)


def _ink_mask(image, box, bg, tolerance=BACKGROUND_TOLERANCE):
    """L-mode mask: 255 where the pixel differs from the background colour."""
    from PIL import Image, ImageChops
    crop = image.crop(box)
    diff = ImageChops.difference(crop, Image.new("RGB", crop.size, bg))
    red, green, blue = diff.split()
    total = ImageChops.add(ImageChops.add(red, green), blue)
    return total.point(lambda v: 255 if v > tolerance else 0)


def _components(mask):
    """8-connected ink components: bounding box, pixel count and border contact."""
    width, height = mask.size
    data = mask.tobytes()
    seen = array("b", [0]) * (width * height)
    comps = []
    index = data.find(b"\xff")
    while index != -1:
        if not seen[index]:
            seen[index] = 1
            queue = deque([index])
            count, touches = 0, False
            x0 = y0 = 1 << 30
            x1 = y1 = -1
            while queue:
                current = queue.popleft()
                count += 1
                y, x = divmod(current, width)
                x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
                if x == 0 or y == 0 or x == width - 1 or y == height - 1:
                    touches = True
                for dy in (-1, 0, 1):
                    ny = y + dy
                    if not 0 <= ny < height:
                        continue
                    for dx in (-1, 0, 1):
                        nx = x + dx
                        if 0 <= nx < width:
                            neighbour = ny * width + nx
                            if data[neighbour] and not seen[neighbour]:
                                seen[neighbour] = 1
                                queue.append(neighbour)
            comps.append({"box": [x0, y0, x1 + 1, y1 + 1], "pixels": count, "touches": touches})
        index = data.find(b"\xff", index + 1)
    return comps


def _clusters(comps):
    """Group components into text lines: vertical overlap, then horizontal proximity."""
    bands = []
    for comp in sorted(comps, key=lambda c: c["box"][1]):
        top, bottom = comp["box"][1], comp["box"][3]
        for band in bands:
            overlap = min(band["box"][3], bottom) - max(band["box"][1], top)
            if overlap >= 0.4 * min(band["box"][3] - band["box"][1], bottom - top):
                band["comps"].append(comp)
                band["box"][1], band["box"][3] = min(band["box"][1], top), max(band["box"][3], bottom)
                break
        else:
            bands.append({"comps": [comp], "box": [0, top, 0, bottom]})
    clusters = []
    for band in bands:
        gap = 0.9 * (band["box"][3] - band["box"][1])
        current = None
        for comp in sorted(band["comps"], key=lambda c: c["box"][0]):
            if current and comp["box"][0] - current[2] <= gap:
                current[0], current[1] = min(current[0], comp["box"][0]), min(current[1], comp["box"][1])
                current[2], current[3] = max(current[2], comp["box"][2]), max(current[3], comp["box"][3])
            else:
                current = list(comp["box"])
                clusters.append(current)
    return clusters


def _anchor(lock, box, width):
    if "left_pct" in lock:
        return "left_pct", box[0] / width * 100
    if "right_pct" in lock:
        return "right_pct", box[2] / width * 100
    return "center_x_pct", (box[0] + box[2]) / 2 / width * 100


def _expected_box(lock, width, height, aspect):
    """Expected ink box in pixels; recomputed when a job predates box_pct."""
    box = lock.get("box_pct")
    if not (isinstance(box, list) and len(box) == 4):
        box = lock_box(lock, lock.get("text") or "字", aspect)["box_pct"]
    return [box[0] * width / 100, box[1] * height / 100, box[2] * width / 100, box[3] * height / 100]


def _search_box(expected, width, height):
    """Generous window: a line drawn far too large must still fall inside it."""
    x0, y0, x1, y1 = expected
    ink = y1 - y0
    return [max(0, int(x0 - 0.08 * width)), max(0, int(y0 - 1.5 * ink)),
            min(width, int(x1 + 0.25 * width) + 1), min(height, int(y1 + 1.5 * ink) + 1)]


def analyze(image, locks, tolerance_height=TOLERANCE_HEIGHT, tolerance_position=TOLERANCE_POSITION):
    """Measure every locked line on one RGB image."""
    width, height = image.size
    aspect = width / height
    specs = []
    for lock in locks:
        spec = validate_spec({k: v for k, v in lock.items() if k in GROUP_KEYS.values()}, lock.get("group", "lock"))
        specs.append({**lock, **spec})
    result = {"items": [], "size": [width, height]}
    if not specs:
        return result
    expected = [_expected_box(lock, width, height, aspect) for lock in specs]
    windows = [_search_box(box, width, height) for box in expected]
    region = [min(w[0] for w in windows), min(w[1] for w in windows), max(w[2] for w in windows), max(w[3] for w in windows)]
    bg, share = _background(image, region)
    result.update(region=region, background=list(bg), background_share=round(share, 3))
    if share < MIN_BACKGROUND_SHARE:
        for lock in specs:
            result["items"].append({"text": lock.get("text"), "group": lock.get("group"), "status": "unlocated",
                                    "reason": "busy_background"})
        return result
    min_pixels = max(4, int(0.00002 * width * height))
    usable = [c for c in _components(_ink_mask(image, region, bg)) if not c["touches"] and c["pixels"] >= min_pixels]
    clusters = [[c[0] + region[0], c[1] + region[1], c[2] + region[0], c[3] + region[1]] for c in _clusters(usable)]
    candidates = []
    for i, (lock, box, window) in enumerate(zip(specs, expected, windows)):
        ink = box[3] - box[1]
        key, target = next((k, lock[k]) for k in ANCHOR_KEYS if k in lock)
        for j, cluster in enumerate(clusters):
            cx, cy, h = (cluster[0] + cluster[2]) / 2, (cluster[1] + cluster[3]) / 2, cluster[3] - cluster[1]
            if not (window[0] <= cx <= window[2] and window[1] <= cy <= window[3] and 0.4 * ink <= h <= 2.5 * ink):
                continue
            score = abs(cluster[1] - box[1]) / height + abs(_anchor(lock, cluster, width)[1] - target) / 100 + abs(h - ink) / height
            candidates.append((score, i, j))
    chosen, used = {}, set()
    for _, i, j in sorted(candidates):
        if i not in chosen and j not in used:
            chosen[i] = j
            used.add(j)
    for i, lock in enumerate(specs):
        item = {"text": lock.get("text"), "group": lock.get("group"),
                "expected": {"ink_height_pct": lock["ink_height_pct"], "top_pct": lock["top_pct"],
                             **{k: lock[k] for k in ANCHOR_KEYS if k in lock}}}
        if i not in chosen:
            item.update(status="unlocated", reason="line_not_isolated")
            result["items"].append(item)
            continue
        box = clusters[chosen[i]]
        key, anchor = _anchor(lock, box, width)
        measured = {"ink_height_pct": round((box[3] - box[1]) / height * 100, 2),
                    "top_pct": round(box[1] / height * 100, 2), key: round(anchor, 2),
                    "width_pct": round((box[2] - box[0]) / width * 100, 2)}
        dh = measured["ink_height_pct"] / lock["ink_height_pct"] - 1
        dx, dy = anchor - lock[key], measured["top_pct"] - lock["top_pct"]
        drift = abs(dh) > tolerance_height or abs(dx) > tolerance_position or abs(dy) > tolerance_position
        item.update(status="drift" if drift else "ok", measured=measured, box=box,
                    deviation={"height": round(dh, 4), "x_pct": round(dx, 2), "y_pct": round(dy, 2)})
        result["items"].append(item)
    return result


def measure(path, locks, **tolerances):
    from PIL import Image
    with Image.open(path) as img:
        image = img.convert("RGB")
    return analyze(image, locks, **tolerances)


# ---------------------------------------------------------------- size template

TEMPLATE_MARGIN = 0.35       # kept around each line, in ink heights: outline, depth and shadow


def _connected_to(mask, box):
    """Ink inside `box` plus the ink connected to it (outline, depth, shadow); nearby artwork is dropped."""
    from PIL import Image
    width, height = mask.size
    data = mask.tobytes()
    keep = bytearray(width * height)
    queue = deque()
    for y in range(max(0, box[1]), min(height, box[3])):
        for x in range(max(0, box[0]), min(width, box[2])):
            index = y * width + x
            if data[index]:
                keep[index] = 255
                queue.append(index)
    while queue:
        y, x = divmod(queue.popleft(), width)
        for ny in (y - 1, y, y + 1):
            for nx in (x - 1, x, x + 1):
                if 0 <= ny < height and 0 <= nx < width:
                    neighbour = ny * width + nx
                    if data[neighbour] and not keep[neighbour]:
                        keep[neighbour] = 255
                        queue.append(neighbour)
    return Image.frombytes("L", (width, height), bytes(keep))


def make_template(image, analysis, margin=TEMPLATE_MARGIN):
    """A canvas of the same size in the lines' background colour holding only the measured lines."""
    from PIL import Image
    width, height = image.size
    bg = tuple(analysis["background"])
    canvas = Image.new("RGB", (width, height), bg)
    for item in analysis["items"]:
        x0, y0, x1, y1 = item["box"]
        pad = int(round(margin * (y1 - y0)))
        region = (max(0, x0 - pad), max(0, y0 - pad), min(width, x1 + pad), min(height, y1 + pad))
        keep = _connected_to(_ink_mask(image, region, bg), (x0 - region[0], y0 - region[1], x1 - region[0], y1 - region[1]))
        canvas.paste(image.crop(region), region[:2], keep)
    return canvas


def moved_template(image, analysis, target, margin=TEMPLATE_MARGIN):
    """The measured line set at another group's anchor and ink height: real lettering for a group
    that sits elsewhere on the canvas, instead of a guide drawn in a system font."""
    from PIL import Image
    width, height = image.size
    bg = tuple(analysis["background"])
    canvas = Image.new("RGB", (width, height), bg)
    x0, y0, x1, y1 = analysis["items"][0]["box"]
    pad = int(round(margin * (y1 - y0)))
    region = (max(0, x0 - pad), max(0, y0 - pad), min(width, x1 + pad), min(height, y1 + pad))
    keep = _connected_to(_ink_mask(image, region, bg), (x0 - region[0], y0 - region[1], x1 - region[0], y1 - region[1]))
    scale = target["ink_height_pct"] * height / 100 / (y1 - y0)
    crop = image.crop(region)
    size = (max(1, round(crop.width * scale)), max(1, round(crop.height * scale)))
    crop, keep = crop.resize(size, Image.LANCZOS), keep.resize(size, Image.LANCZOS)
    ink_width = (x1 - x0) * scale
    if "left_pct" in target:
        left = target["left_pct"] * width / 100
    elif "right_pct" in target:
        left = target["right_pct"] * width / 100 - ink_width
    else:
        left = target["center_x_pct"] * width / 100 - ink_width / 2
    top = target["top_pct"] * height / 100
    canvas.paste(crop, (round(left - (x0 - region[0]) * scale), round(top - (y0 - region[1]) * scale)), keep)
    return canvas


def template(jobs, page, groups, out, overwrite=False, as_group=None, **tolerances):
    """Cut the locked lines of `groups` out of the page the user picked. The user's eye decides
    which size is right; where it differs from the lock, the lock is updated to match.

    With `as_group`, the one line is set at that group's position and height instead: the template
    for a group whose titles sit elsewhere (right, centre) made from the same approved lettering."""
    from PIL import Image
    job = next((item for item in jobs if item.get("page") == page), None)
    if job is None:
        raise ValueError(f"P{page} is not in the manifests")
    locks = [lock for lock in job.get("text_locks") or [] if lock.get("group") in groups]
    missing = sorted(set(groups) - {lock["group"] for lock in locks})
    if missing:
        raise ValueError(f"P{page} has no line locked to {'、'.join(missing)}")
    target = None
    if as_group:
        if len(locks) != 1:
            raise ValueError("--as-group moves one line: give one --group with a single locked line on that page")
        target = next((lock for item in jobs for lock in item.get("text_locks") or [] if lock.get("group") == as_group), None)
        if target is None:
            raise ValueError(f"No page in the manifests uses 字号锁 {as_group}; declare the group and tag its lines first")
    source, out = Path(job["output"]), Path(out)
    if out.resolve() == source.resolve():
        raise ValueError("The template must not overwrite the slide it is cut from")
    if out.exists() and not overwrite:
        raise ValueError(f"{out} exists; pages that use it would all change. Pass --overwrite to replace it")
    with Image.open(source) as img:
        image = img.convert("RGB")
    analysis = analyze(image, locks, **tolerances)
    unlocated = [item for item in analysis["items"] if item["status"] == "unlocated"]
    if unlocated:
        raise ValueError(f"P{page}: cannot find " + "、".join(f"“{item['text']}” ({item.get('reason')})" for item in unlocated)
                         + "; the line needs a flat background around it. Pick another page.")
    out.parent.mkdir(parents=True, exist_ok=True)
    (moved_template(image, analysis, target) if target else make_template(image, analysis)).save(out)
    updates = []
    for item in analysis["items"]:
        if target:  # scaled to the other group's own height: neither lock changes
            break
        if item["status"] == "drift":  # the chosen page is the standard now; the 字框 must agree with it
            measured = item["measured"]
            anchor = next(key for key in ANCHOR_KEYS if key in measured)
            label = {"left_pct": "左", "center_x_pct": "居中", "right_pct": "右"}[anchor]
            updates.append({"group": item["group"], "ink_height_pct": measured["ink_height_pct"], "top_pct": measured["top_pct"],
                            anchor: measured[anchor],
                            "change": f"字号锁 {item['group']}：字高 {_pct(measured['ink_height_pct'])}，顶 {_pct(measured['top_pct'])}，"
                                      f"{label} {_pct(measured[anchor])}（其余不变）"})
    steps = [f"在常量块 {'、'.join('字号锁 ' + group for group in ([as_group] if target else groups))} 这一行末尾加“，样板 {out}”"]
    if updates:
        steps.append("这一页的字和原锁定值不同，以这一页为准，把锁定值改成：" + "；".join(item["change"] for item in updates))
    steps.append("再运行 extract_briefs.py：用这组字的每一页都会附上样板。")
    return {"template": str(out), "page": page, "source": str(source),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "lines": [{"group": item["group"], "text": item["text"], "status": item["status"], "measured": item["measured"]}
                      for item in analysis["items"]],
            "lock_updates": updates, "next": "；".join(steps)}


# ---------------------------------------------------------------- native geometry guide

def guide(canvas, text, font, ink_height, left, top, out,
          background="#FFFFFF", color="#000000", overwrite=False):
    """Render one native-font line on a new full-size geometry-only canvas.

    Font point size is calibrated against rendered ink, not treated as ink
    height. The output is measured with the normal detector before either file
    is published; a failed render/measurement leaves no output pair behind.
    """
    from PIL import Image, ImageColor, ImageFont

    if isinstance(canvas, str):
        matched = re.fullmatch(r"([1-9][0-9]*)[xX]([1-9][0-9]*)", canvas)
        if not matched:
            raise ValueError("guide --canvas needs WIDTHxHEIGHT, e.g. 1672x941")
        canvas = tuple(map(int, matched.groups()))
    if (not isinstance(canvas, (list, tuple)) or len(canvas) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in canvas)):
        raise ValueError("guide canvas must contain two positive pixel dimensions")
    width, height = canvas
    if width * height > 40_000_000:
        raise ValueError("guide canvas exceeds 40 million pixels")
    if (not isinstance(text, str) or not text.strip()
            or any(unicodedata.category(c).startswith("C") or c in "\u2028\u2029" for c in text)):
        raise ValueError("guide --text must be a nonempty single line without control characters")
    spec = validate_spec({"ink_height_pct": ink_height, "left_pct": left, "top_pct": top}, "guide")
    if not all(math.isfinite(v) for v in spec.values()):
        raise ValueError("guide geometry must contain finite percentages")
    target_height = height * spec["ink_height_pct"] / 100
    if target_height < 4:
        raise ValueError("guide ink height must span at least four pixels to be measurable")
    font_path, out = Path(font), Path(out)
    if not font_path.is_file():
        raise ValueError(f"guide font is not a file: {font_path}")
    metadata = out.with_suffix(".json")
    if out.suffix.lower() != ".png":
        raise ValueError("guide --out must name a .png file; its metadata uses the same stem with .json")
    if any(path.resolve() == font_path.resolve() for path in (out, metadata)):
        raise ValueError("guide outputs must not overwrite the input font")
    for path in (out, metadata):
        if path.is_symlink():
            raise ValueError(f"guide output must not be a symbolic link: {path}")
        if path.exists() and (not overwrite or not path.is_file()):
            raise ValueError(f"{path} exists; pass --overwrite to replace guide outputs")
    try:
        bg, fg = ImageColor.getrgb(background), ImageColor.getrgb(color)
    except (ValueError, TypeError) as exc:
        raise ValueError("guide background and color must be valid opaque RGB colors") from exc
    if len(bg) != 3 or len(fg) != 3 or sum(abs(a - b) for a, b in zip(bg, fg)) <= BACKGROUND_TOLERANCE:
        raise ValueError("guide colors need enough opaque RGB contrast for ink measurement")

    def raster(size):
        native = ImageFont.truetype(str(font_path), size=size)
        mask = native.getmask(text, mode="L")
        if not all(mask.size) or mask.size[0] * mask.size[1] > 40_000_000:
            raise ValueError("guide text is empty or too large to render on this canvas")
        alpha = Image.frombytes("L", mask.size, bytes(mask))
        painted = Image.composite(Image.new("RGB", mask.size, fg), Image.new("RGB", mask.size, bg), alpha)
        box = _ink_mask(painted, (0, 0, *painted.size), bg).getbbox()
        if box is None:
            raise ValueError("guide text has no measurable native-font ink")
        return native, painted.crop(box)

    # Find the nearest true raster height, then inspect nearby sizes for pixel
    # hinting/rounding. Never shrink a line merely to make it fit horizontally.
    low, high = 1, max(8, math.ceil(target_height * 2))
    while raster(high)[1].height < target_height:
        high *= 2
        if high > max(1024, height * 4):
            raise ValueError("guide font cannot reach the requested ink height")
    while low < high:
        middle = (low + high) // 2
        if raster(middle)[1].height < target_height:
            low = middle + 1
        else:
            high = middle
    best = None
    for size in range(max(1, low - 3), low + 4):
        native, painted = raster(size)
        rank = (abs(painted.height - target_height), size)
        if best is None or rank < best[0]:
            best = (rank, size, native, painted)
    _, font_size, native, painted = best
    x, y = round(width * spec["left_pct"] / 100), round(height * spec["top_pct"] / 100)
    ink_box = [x, y, x + painted.width, y + painted.height]
    if ink_box[2] > width or ink_box[3] > height:
        raise ValueError("guide text leaves the canvas at the requested ink height and position")
    image = Image.new("RGB", (width, height), bg)
    image.paste(painted, (x, y))
    lock = {"text": text, "group": "guide", **spec,
            "box_pct": [spec["left_pct"], spec["top_pct"],
                        spec["left_pct"] + painted.width / width * 100,
                        spec["top_pct"] + spec["ink_height_pct"]]}

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{out.stem}-guide-", dir=out.parent) as directory:
        temporary = Path(directory)
        png_temp, json_temp = temporary / "guide.png", temporary / "guide.json"
        image.save(png_temp, format="PNG")
        # This is stricter than the existing position default; no tolerance is
        # relaxed to make a generated guide pass its own calibration.
        tolerances = {"tolerance_height": TOLERANCE_HEIGHT,
                      "tolerance_position": min(TOLERANCE_POSITION, max(100 / width, 100 / height))}
        measured = measure(png_temp, [lock], **tolerances)
        item = measured["items"][0]
        if item["status"] != "ok" or item.get("box") != ink_box:
            raise ValueError("guide calibration failed: the normal detector could not verify the complete requested line")
        result = {"command": "guide", "schema_version": 1, "purpose": "geometry-only",
                  "scope": "Scale and position only; keep glyph style from the target slide or its typography reference.",
                  "output": str(out), "metadata": str(metadata), "canvas": [width, height], "text": text,
                  "font": str(font_path.resolve()), "font_sha256": hashlib.sha256(font_path.read_bytes()).hexdigest(),
                  "font_size": font_size, "font_name": list(native.getname()),
                  "background": list(bg), "color": list(fg),
                  "requested": spec, "ink_box": ink_box,
                  "ink_height_pct": painted.height / height * 100,
                  "left_pct": x / width * 100, "top_pct": y / height * 100,
                  "width_pct": painted.width / width * 100,
                  "image_sha256": hashlib.sha256(png_temp.read_bytes()).hexdigest(),
                  "measurement_tolerances": tolerances, "measurement": measured}
        json_temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pairs = [(png_temp, out), (json_temp, metadata)]
        backups, published = {}, []
        try:
            if overwrite:
                for index, (_, destination) in enumerate(pairs):
                    if destination.exists():
                        backup = temporary / f"before-{index}"
                        backup.write_bytes(destination.read_bytes())
                        backups[destination] = backup
            for source, destination in pairs:
                if overwrite:
                    os.replace(source, destination)
                else:
                    # Linking is atomic and refuses a concurrently created
                    # destination; both files are complete before publication.
                    os.link(source, destination)
                published.append(destination)
        except OSError:
            for destination in reversed(published):
                if destination in backups:
                    os.replace(backups[destination], destination)
                else:
                    destination.unlink(missing_ok=True)
            raise
    return result


# ---------------------------------------------------------------- feedback loop

def feedback_note(item, tolerance_height=TOLERANCE_HEIGHT, tolerance_position=TOLERANCE_POSITION):
    """One sentence for the next brief: what was off last time, in the brief's own units."""
    deviation, measured, expected = item["deviation"], item["measured"], item["expected"]
    parts = []
    if abs(deviation["height"]) > tolerance_height:
        direction = "大" if deviation["height"] > 0 else "小"
        parts.append(f"字画{direction}了：字高是画布高度的 {_pct(measured['ink_height_pct'])}，应为 "
                     f"{_pct(expected['ink_height_pct'])}，{direction}了 {abs(deviation['height']) * 100:.0f}%")
    if abs(deviation["y_pct"]) > tolerance_position:
        parts.append(f"位置偏{'下' if deviation['y_pct'] > 0 else '上'}了画布高度的 {_pct(abs(deviation['y_pct']))}")
    if abs(deviation["x_pct"]) > tolerance_position:
        parts.append(f"位置偏{'右' if deviation['x_pct'] > 0 else '左'}了画布宽度的 {_pct(abs(deviation['x_pct']))}")
    return (f"“{item['text']}”上次{'；'.join(parts)}。这次以规定的墨迹字高和定位锚点为准，"
            "保留字形比例与自然行宽。")


def load_feedback(path):
    """{page number: [notes]} from the feedback file; missing file means no feedback."""
    path = Path(path)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    pages = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, dict):
        raise ValueError(f"{path}: expected an object with pages")
    notes = {}
    for key, entry in pages.items():
        if not re.fullmatch(r"[0-9]+", str(key)) or not isinstance(entry, dict) or not isinstance(entry.get("items"), list):
            raise ValueError(f"{path}: page entries need a numeric key and an items list")
        notes[int(key)] = entry["items"]
    return notes


def update_feedback(path, pages, tolerances):
    """Record drift per page. A note is kept once its page turns ok, so the brief that
    produced the accepted image never changes and the page is not redrawn again."""
    path = Path(path)
    data = {"schema_version": 1, "pages": {}}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or not isinstance(loaded.get("pages"), dict):
            raise ValueError(f"{path}: expected an object with pages")
        data = loaded
    changed = []
    for page in pages:
        drifted = [item for item in page["items"] if item["status"] == "drift"]
        key = str(page["page"])
        previous = data["pages"].get(key, {})
        if not drifted or previous.get("image_sha256") == page["image_sha256"]:
            continue
        data["pages"][key] = {
            "image_sha256": page["image_sha256"], "attempt": int(previous.get("attempt", 0)) + 1,
            "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "items": [{"text": item["text"], "group": item["group"], "note": feedback_note(item, **tolerances)}
                      for item in drifted]}
        changed.append(page["page"])
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed, {int(key): entry.get("attempt", 1) for key, entry in data["pages"].items()}


# ---------------------------------------------------------------- CLI

def load_jobs(paths):
    jobs = []
    for path in paths:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"{path}: manifest must be a jobs array")
        jobs.extend(data)
    pages = [job.get("page") for job in jobs]
    if len(pages) != len(set(pages)):
        raise ValueError("Duplicate pages across manifests")
    return sorted(jobs, key=lambda job: job.get("page") or 0)


def check(jobs, **tolerances):
    pages = []
    for job in jobs:
        locks = job.get("text_locks") or []
        if not locks:
            continue
        path = Path(job["output"])
        if not path.is_file():
            pages.append({"page": job.get("page"), "output": job["output"], "status": "missing", "items": []})
            continue
        measured = measure(path, locks, **tolerances)
        statuses = {item["status"] for item in measured["items"]}
        pages.append({"page": job.get("page"), "output": job["output"],
                      "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "status": "drift" if "drift" in statuses else "unlocated" if "unlocated" in statuses else "ok",
                      **measured})
    return pages


def group_spread(pages):
    """Per group: measured ink height range across pages, the direct consistency view."""
    heights = {}
    for page in pages:
        for item in page["items"]:
            if "measured" in item:
                heights.setdefault(item["group"], []).append(item["measured"]["ink_height_pct"])
    return {group: {"lines": len(values), "min_pct": min(values), "max_pct": max(values),
                    "spread": round(max(values) / min(values) - 1, 4)} for group, values in heights.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["check", "template", "guide"])
    parser.add_argument("--manifest", action="append")
    parser.add_argument("--page", type=int, help="template: the approved page to cut the locked lines from")
    parser.add_argument("--group", action="append", help="template: lock group(s) to include; repeatable")
    parser.add_argument("--as-group", help="template: set the one cut line at this other group's position and height, "
                                           "for a group whose titles sit elsewhere on the canvas")
    parser.add_argument("--out", help="template/guide: output PNG, e.g. assets/typography/size-title.png")
    parser.add_argument("--overwrite", action="store_true", help="template/guide: explicitly replace existing outputs")
    parser.add_argument("--canvas", help="guide: full canvas WIDTHxHEIGHT, e.g. 1672x941")
    parser.add_argument("--text", help="guide: the exact single line to render, without labels or marks")
    parser.add_argument("--font", help="guide: path to a native TTF/OTF/TTC font containing the needed glyphs")
    parser.add_argument("--ink-height", type=float, help="guide: actual rendered ink height as percent of canvas height")
    parser.add_argument("--left", type=float, help="guide: ink left edge as percent of canvas width")
    parser.add_argument("--top", type=float, help="guide: ink top edge as percent of canvas height")
    parser.add_argument("--background", default="#FFFFFF", help="guide: flat opaque background color")
    parser.add_argument("--color", default="#000000", help="guide: opaque text color")
    parser.add_argument("--report", help="Write the full JSON report here")
    parser.add_argument("--feedback", help="Record drift here for the --pages the user chose to redraw; "
                                           "the next extract_briefs.py run adds it to those briefs")
    parser.add_argument("--pages", help="With --feedback: pages the user chose to redraw, e.g. 6,9, or all")
    parser.add_argument("--tolerance-height", type=float, default=TOLERANCE_HEIGHT,
                        help=f"Allowed ink-height deviation as a fraction (default {TOLERANCE_HEIGHT})")
    parser.add_argument("--tolerance-position", type=float, default=TOLERANCE_POSITION,
                        help=f"Allowed shift in percent of the canvas side (default {TOLERANCE_POSITION})")
    args = parser.parse_args(argv)
    if args.command == "guide":
        required = ("canvas", "text", "font", "ink_height", "left", "top", "out")
        missing = ["--" + name.replace("_", "-") for name in required if getattr(args, name) is None]
        if missing:
            parser.error("guide needs " + ", ".join(missing))
        if args.manifest or args.page or args.group or args.feedback or args.pages or args.report:
            parser.error("guide renders a new canvas; manifest, page, group, feedback and report options belong to check/template")
        try:
            result = guide(args.canvas, args.text, args.font, args.ink_height, args.left, args.top, args.out,
                           args.background, args.color, args.overwrite)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"text_lock failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not args.manifest:
        parser.error("check/template need --manifest")
    tolerances = {"tolerance_height": args.tolerance_height, "tolerance_position": args.tolerance_position}
    chosen = None
    if args.feedback:
        if not args.pages:
            parser.error("--feedback needs --pages: redrawing costs money, so record notes only for the pages "
                         "the user chose after looking at them (e.g. --pages 6,9, or --pages all if they said so)")
        if args.pages.strip().lower() != "all":
            try:
                chosen = {int(part) for part in args.pages.replace("，", ",").split(",") if part.strip()}
            except ValueError:
                parser.error("--pages takes page numbers such as 6,9, or all")
    if args.command == "template":
        if not (args.page and args.group and args.out):
            parser.error("template needs --page, --group and --out")
        try:
            result = template(load_jobs(args.manifest), args.page, args.group, args.out, args.overwrite,
                              as_group=args.as_group, **tolerances)
        except (OSError, ValueError, KeyError) as exc:
            print(f"text_lock failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    try:
        pages = check(load_jobs(args.manifest), **tolerances)
        report = {"command": "check", "tolerances": tolerances, "pages": pages, "groups": group_spread(pages),
                  "ok": all(page["status"] == "ok" for page in pages)}
        if args.feedback:
            located = [page for page in pages if page["status"] in {"ok", "drift", "unlocated"}
                       and (chosen is None or page["page"] in chosen)]
            changed, attempts = update_feedback(args.feedback, located, tolerances)
            report["feedback"] = {"file": args.feedback, "pages_with_new_feedback": changed, "attempts": attempts}
    except (OSError, ValueError, KeyError) as exc:
        print(f"text_lock failed: {exc}", file=sys.stderr)
        return 2
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {"ok": report["ok"], "groups": report["groups"], "pages": [
        {"page": page["page"], "status": page["status"],
         "lines": [{"text": item["text"], "status": item["status"],
                    **({"deviation": item["deviation"]} if "deviation" in item else {"reason": item.get("reason")})}
                   for item in page["items"]]} for page in pages]}
    if "feedback" in report:
        summary["feedback"] = report["feedback"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
