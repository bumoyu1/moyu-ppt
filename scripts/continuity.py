#!/usr/bin/env python3
"""Page-to-page continuity in design-plans.md.

Each page says how it connects to its neighbours, above its spatial brief:

    **[承接与递进]**
    - 承接上页：沿用上一页的岛屿与人物，标题位置不变
    - 本页变化：换成俯视，主体移到右下
    - 铺垫下页：留出右侧的路，下一页沿路展开三个维度

This field is for whoever writes the plan; it never reaches the image model.
The model sees one page at a time and has never seen the previous one, so
"different from the last page" means nothing to it. Whatever 本页变化 says has
to be written into the page's own **构图方式** line: where the subject sits, from
what viewpoint, where the visual weight falls.

Rules (decks whose workflow has continuity_rules):
- every page writes all three items; only page 1 may write 承接上页：不适用 and
  only the last page 铺垫下页：不适用;
- 稳定 pages may reuse the previous page's skeleton (parallel steps read better
  that way);
- 封面 (the first page), 章节页 and 结尾 are showcase pages: never 稳定, and each writes a 构图方式 of its
  own, different from every other showcase page (three chapter dividers on one layout
  are what makes a deck look the same from start to end);
- a long run of 稳定 content pages is reported as a warning: 稳定 is for pages meant to
  be compared side by side, a new point is a 变奏 page;
- 变奏 and 亮点 pages need a **构图方式** line in the spatial brief that is not a
  copy of the previous page's. A 构图方式 that never says where things sit (no
  placement word, no coordinates) is reported as a warning. Viewpoint is not
  required here: many decks lock it in the constants block for every page.
"""
from __future__ import annotations

import re

LABELS = ("承接上页", "本页变化", "铺垫下页")
NOT_APPLICABLE = "不适用"
PLACEHOLDERS = {"", "无", "同上", "略", "暂无", "-", "—", "——", "/"}
FIELD_RE = re.compile(r"\*\*\[承接与递进\]\*\*[ \t]*[:：]?(.*?)(?=\n[ \t]*\*\*\[|\Z)", re.S)
LABEL_RE = re.compile(r"(承接上页|本页变化|铺垫下页)[ \t]*[:：]")
COMPOSITION_RE = re.compile(r"\*\*构图方式\*\*[ \t]*[:：][ \t]*([^\r\n]*)")
DISPLAY_KINDS = {"cover": "封面", "divider": "章节页", "ending": "结尾"}  # by [内容来源]
STABLE_RUN = 3  # more consecutive 稳定 content pages than this draw a warning
PLACE_WORDS = ("%", "重心", "焦点", "视觉中心", "居中", "中央", "偏左", "偏右", "左侧", "右侧", "左半", "右半", "左下",
               "右下", "左上", "右上", "上方", "下方", "上半", "下半", "三分", "对角", "左右", "上下")


def parse_field(section):
    """{label: text} from a page's [承接与递进]; None when the page has no such field."""
    head = section.split("**[空间定位图]**", 1)[0]
    match = FIELD_RE.search(head)
    if not match:
        return None
    body = match.group(1)
    marks = list(LABEL_RE.finditer(body))
    items = {}
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(body)
        items[mark.group(1)] = body[mark.end():end].strip(" \t\r\n-*；;。")
    return items


def composition(section):
    """The **构图方式** line of the spatial brief (what the model actually reads), or None."""
    parts = section.split("**[空间定位图]**", 1)
    match = COMPOSITION_RE.search(parts[1]) if len(parts) == 2 else None
    return match.group(1).strip() if match else None


def _bare(text):
    return re.sub(r"[\s，,。.；;：:、（）()“”\"'【】\[\]]", "", text or "")


def check_plan(sections, rhythms, kinds=None):
    """sections: [(number, section text)]; rhythms: {number: stable|variation|highlight};
    kinds: {number: cover|toc|divider|ending|recap|nodes} from each page's [内容来源], when known.
    Returns (problems, warnings, summary)."""
    problems, warnings = [], []
    kinds = kinds or {}
    pages = dict(sections)
    numbers = sorted(pages)
    first, last = 1, max(numbers) if numbers else 0
    for number in numbers:
        items = parse_field(pages[number])
        if items is None:
            problems.append(f"P{number} 没写 [承接与递进]：写承接上页、本页变化、铺垫下页三项。"
                            "这一栏不进提示词，用来想清楚前后页怎么接。")
            continue
        for label in LABELS:
            value = items.get(label)
            allowed = (label == "承接上页" and number == first) or (label == "铺垫下页" and number == last)
            if value is None or value in PLACEHOLDERS:
                problems.append(f"P{number} [承接与递进] 缺“{label}”" + ("（这一页可以写“不适用”）" if allowed else ""))
            elif value == NOT_APPLICABLE and not allowed:
                problems.append(f"P{number} 的“{label}”写了不适用：只有第一页可以不承接、最后一页可以不铺垫")
    previous = None
    for number in numbers:
        rhythm = rhythms.get(number, "stable")
        layout = composition(pages[number])
        if rhythm in ("variation", "highlight"):
            kind = "变奏页" if rhythm == "variation" else "亮点页"
            if not layout:
                problems.append(f"P{number} 是{kind}，空间定位图里要有一行 **构图方式**：写清主体在哪、什么视角、重心在哪。"
                                "和上一页的区别要落在这一行，模型才看得到。")
            else:
                if previous and previous[0] == number - 1 and previous[1] and _bare(layout) == _bare(previous[1]):
                    problems.append(f"P{number} 是{kind}，构图方式和 P{number - 1} 一字不差；把“本页变化”写进构图方式。")
                if not any(word in layout for word in PLACE_WORDS):
                    warnings.append(f"P{number}（{kind}）的构图方式没写主体在哪、重心在哪；"
                                    "只写“换个构图”，模型不知道换成什么。")
        previous = (number, layout)
    # Showcase pages carry little text and the most freedom; they are also where a deck turns uniform.
    showcase = {}
    for number in numbers:
        # The first page is the cover whatever its source says (a course title is often a node of the skeleton).
        name = DISPLAY_KINDS.get(kinds.get(number)) or ("封面" if number == first and any(kinds.values()) else None)
        if not name:
            continue
        if rhythms.get(number, "stable") == "stable":
            problems.append(f"P{number} 是{name}，属于展示页，[节奏] 不能是稳定（没写也算稳定）。写成“亮点（{name}）”，"
                            "并在构图方式里写它自己的画面：大字放哪、多大，主体是什么景别。展示页不套内容页的骨架。")
            continue
        layout = _bare(composition(pages[number]) or "")
        if layout and layout in showcase:
            problems.append(f"P{number}（{name}）的构图方式和 P{showcase[layout]} 一字不差。展示页各有各的构图："
                            "换景别、换大字的位置和大小、换主体在做什么。")
        showcase.setdefault(layout, number)
    run = []
    for number in numbers + [None]:
        if number is not None and kinds.get(number) == "nodes" and rhythms.get(number, "stable") == "stable":
            run.append(number)
            continue
        if len(run) > STABLE_RUN:
            warnings.append(f"P{run[0]}–P{run[-1]} 连续 {len(run)} 页都是稳定页。稳定页只用于同口径比较或逐步讲解；"
                            "其中讲新论点的页改成变奏，写出它自己的构图。")
        run = []
    summary = {"pages": len(numbers),
               "showcase": [n for n in numbers if kinds.get(n) in DISPLAY_KINDS or (n == first and any(kinds.values()))],
               "variation_or_highlight": [n for n in numbers if rhythms.get(n) in ("variation", "highlight")]}
    return problems, warnings, summary
