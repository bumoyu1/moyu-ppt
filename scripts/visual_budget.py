#!/usr/bin/env python3
"""Image/text budget: one deck-wide ratio that most pages actually follow.

The user confirms a base ratio at the brief (for example 85/15, picture/text)
and a floor, by default 5 points below it (80/20). Each page of
design-plans.md states its own budget:

    **[融合比]**：85/15
    **[融合比]**：80/20：四要素各配一句，必须完整保留

Rules checked at split time (extract_briefs.py):
- every page states a budget, picture + text = 100;
- no page goes below the floor; when the text still does not fit, shorten it or
  split the page, never shrink the type;
- a page below the base gives its reason after the ratio;
- at least two thirds of the pages sit at the base or above.
Cover, chapter and ending pages (by [内容来源]) only answer to the floor: their
big lettering is the picture, so they are left out of the two-thirds share and
get no area budget in the brief.

The page budget is written into the brief the model sees, in plain words
("this page is about 85% picture; title, labels, captions and data take no
more than 15%"). The model handles the balance from there; nothing is
measured after generation.
"""
from __future__ import annotations

import math
import re

DEFAULT_FLOOR_GAP = 5         # floor = base - 5 points of picture share
BASE_SHARE = 2 / 3            # content pages that must sit at the base or above
BUDGET_FIELD_RE = re.compile(r"\*\*\[融合比\]\*\*[ \t]*[:：][ \t]*([^\r\n]*)")
RATIO_RE = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{1,2})\s*(?:[：:，,；;。.\s]+(.*))?$")
# Pages whose display lettering is the picture: they only answer to the floor.
LETTERING = {"cover", "divider", "ending"}


def parse_ratio(value):
    """'85/15' -> (85, 15); anything else -> None."""
    match = RATIO_RE.match(value or "")
    if not match:
        return None
    picture, text = int(match.group(1)), int(match.group(2))
    return (picture, text) if picture > 0 and text > 0 and picture + text == 100 else None


def base_and_floor(brief):
    """(base picture share, floor) from the confirmed brief; None for per-page or unset."""
    ratio = parse_ratio(str((brief or {}).get("visual_ratio") or ""))
    if not ratio:
        return None
    floor = (brief or {}).get("visual_floor")
    if isinstance(floor, bool) or not isinstance(floor, int):
        floor = ratio[0] - DEFAULT_FLOOR_GAP
    return ratio[0], floor


def page_budget(section):
    """{'picture', 'text', 'reason'} from a page's [融合比] field, or an error string."""
    field = BUDGET_FIELD_RE.search(section.split("**[空间定位图]**", 1)[0])
    if not field:
        return "没有写 [融合比]"
    match = RATIO_RE.match(field.group(1))
    ratio = parse_ratio(field.group(1))
    if not ratio:
        return f"[融合比]“{field.group(1).strip()}”看不懂；写成“画面/文字”，两数之和为 100，如 85/15"
    return {"picture": ratio[0], "text": ratio[1], "reason": (match.group(3) or "").strip()}


def check_plan(pages, base, floor, partial=False):
    """pages: {number: {'budget': dict|str, 'kind': str|None}}. Returns (problems, warnings, summary).
    partial: only some pages are being split (pilots), so a short two-thirds share is a warning."""
    problems, warnings = [], []
    content = []
    for number in sorted(pages):
        budget, kind = pages[number]["budget"], pages[number].get("kind")
        if isinstance(budget, str):
            problems.append(f"P{number} {budget}")
            continue
        if budget["picture"] < floor:
            problems.append(
                f"P{number} 写的是 {budget['picture']}/{budget['text']}，低于下限 {floor}/{100 - floor}：把这一页的文字写短，"
                "或者拆成两页；不靠缩小字号塞进去")
        elif budget["picture"] < base and not budget["reason"]:
            problems.append(f"P{number} 低于基准 {base}/{100 - base}，要在比例后面写明原因，如“{floor}/{100 - floor}：四要素各配一句，须完整保留”")
        if kind not in LETTERING:
            content.append((number, budget["picture"]))
    at_base = [number for number, picture in content if picture >= base]
    need = math.ceil(len(content) * BASE_SHARE) if content else 0
    if content and len(at_base) < need:
        below = [f"P{number}" for number, picture in content if picture < base]
        (warnings if partial else problems).append(
            f"除封面、章节页、结尾页外的 {len(content)} 页里只有 {len(at_base)} 页达到基准 {base}/{100 - base}，"
            f"至少要 {need} 页。低于基准的页：{'、'.join(below)}。把其中一些页的文字减到基准，或者拆页")
    summary = {"base": f"{base}/{100 - base}", "floor": f"{floor}/{100 - floor}", "content_pages": len(content),
               "at_base": len(at_base), "below_base": [number for number, picture in content if picture < base]}
    return problems, warnings, summary


def brief_appendix(budget):
    if not isinstance(budget, dict):
        return ""
    return ("\n图文预算（尺寸指令，不是画面文字）：这一页画面约占 {picture}%，标题、标签牌、说明框、引语和数据"
            "加起来不超过画面的 {text}%。标签牌和说明框贴紧文字，不留大块空白；放不下时不要把字缩小，"
            "也不要加大标签牌。").format(**budget)
