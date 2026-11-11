#!/usr/bin/env python3
"""Page copy: every line that will appear on a slide, confirmed before any design.

The skeleton (content-map.md) says what each section means. It does not say
which words reach the screen, so without this step the user first reads a
page's text on the finished image, and a line nobody asked for (a quotation, a
closing slogan, a summary strip) is found only then. `page-copy.md` lists, page
by page, the exact on-screen text and where each line comes from:

    ## 第3页
    - 内容来源：A1+A2
    - 上屏：为什么要学冲煮｜压缩
    - 上屏：80%来自咖啡豆｜原文
    - 上屏：01｜新增：步骤序号
    - 上屏：一杯好咖啡的味道｜引语：用户要求保留这一句

Marks after the last ｜:

    原文         verbatim from the source (checked)
    压缩         the section's own content, shortened
    新增：理由    not in the source; the reason is required
    引语：理由    a quotation, slogan or attribution; verbatim (checked). In a
                 知识型 deck it is on screen only for a stated reason.

    page_copy.py check --copy page-copy.md --map content-map.md \
        [--state deck-state.json] [--plans design-plans.md]

`check` verifies the format and the marks, aligns the pages with the skeleton
by the rules of content_map.py (nothing dropped silently, chapters stay
together, source order) and, with --plans, that every page of design-plans.md
shows exactly these lines. It cannot judge wording: the user reads the sheet.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import content_map

PAGE_RE = re.compile(r"^#{2,4}[ \t]*第[ \t]*(\d+)[ \t]*页(?:[ \t]+.*)?$")
SOURCE_RE = re.compile(r"^[ \t]*[-*][ \t]*内容来源[ \t]*[：:][ \t]*(.*?)[ \t]*$")
LINE_RE = re.compile(r"^[ \t]*[-*][ \t]*上屏[ \t]*[：:][ \t]*(.*?)[ \t]*$")
MARK_RE = re.compile(r"^(原文|压缩|新增|引语)(?:[ \t]*[：:][ \t]*(.*))?$")
MARKS = ("原文", "压缩", "新增", "引语")
VERBATIM = ("原文", "引语")
TITLE_SPREAD = 2  # content-page titles further apart than this many characters draw a warning
FORMAT_HINT = "写成“- 上屏：文字｜原文”，出处写 原文、压缩、新增：理由 或 引语：理由"


def parse_copy(text):
    """Return (pages, problems). pages: {number: {raw, lines: [{text, mark, reason}]}}."""
    pages, problems = {}, []
    current = None
    for number, line in enumerate(text.splitlines(), 1):
        match = PAGE_RE.match(line)
        if match:
            page = int(match.group(1))
            if page in pages:
                problems.append(f"第 {number} 行：第 {page} 页重复")
            current = pages.setdefault(page, {"raw": None, "lines": []})
            continue
        if line.startswith("#"):
            current = None
            continue
        if current is None:
            continue
        match = SOURCE_RE.match(line)
        if match:
            current["raw"] = match.group(1)
            continue
        match = LINE_RE.match(line)
        if not match:
            continue
        value = match.group(1)
        cut = max(value.rfind("｜"), value.rfind("|"))
        shown, tail = (value[:cut].strip(), value[cut + 1:].strip()) if cut >= 0 else (value.strip(), "")
        mark = MARK_RE.match(tail)
        if not shown:
            problems.append(f"第 {number} 行：上屏文字是空的")
        elif not mark:
            problems.append(f"第 {number} 行：“{shown}”没有标出处；{FORMAT_HINT}")
        elif "；" in shown or ";" in shown:
            problems.append(f"第 {number} 行：上屏文字里不能有分号（方案的可见文字清单用分号分隔字段）；拆成两行或换成逗号")
        else:
            current["lines"].append({"text": shown, "mark": mark.group(1), "reason": (mark.group(2) or "").strip()})
    if not pages:
        problems.append("没有页面；每页写成“## 第1页”，下面一行“- 内容来源：…”和若干行“- 上屏：文字｜出处”")
    return pages, problems


def as_plan_pages(pages):
    """The page copy in the shape content_map.check_pages reads."""
    return {number: {"raw": page["raw"] or "", "copy": [line["text"] for line in page["lines"]],
                     **content_map.parse_page_source(page["raw"] or "")}
            for number, page in pages.items()}


def check_copy(copy_path, map_path, state_path=None, page_count=None):
    """Validate the page copy against the skeleton and the source. Returns a report with `problems`."""
    copy_path = Path(copy_path)
    try:
        pages, problems = parse_copy(copy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        return {"ok": False, "problems": [f"读不到文案稿 {copy_path}（{type(exc).__name__}）"], "warnings": [], "pages": None}
    warnings = []
    checked = content_map.check_map(map_path, state_path) if map_path else {"ok": False, "skeleton": None, "problems": []}
    skeleton = checked.get("skeleton")
    if skeleton is None or not checked["ok"]:
        problems.append("内容骨架还没通过检查；文案稿对着骨架写，先修好骨架："
                        + "；".join(checked.get("problems", [])[:3] or ["没有登记内容骨架"]))
        skeleton = None
    numbers = sorted(pages)
    if numbers and numbers != list(range(1, len(numbers) + 1)):
        problems.append(f"页码要从第 1 页连续排到第 {len(numbers)} 页；现在是 {'、'.join(map(str, numbers))}")
    if isinstance(page_count, int) and not isinstance(page_count, bool) and page_count > 0 and numbers \
            and len(numbers) != page_count:
        problems.append(f"文案稿有 {len(numbers)} 页，已确认的页数是 {page_count} 页；改文案，或先和用户改页数")
    source = content_map.source_text(skeleton, map_path, state_path) if skeleton else None
    knowledge = bool(skeleton) and skeleton["type"] == "知识型"
    for number in numbers:
        page = pages[number]
        if not (page["raw"] or "").strip():
            problems.append(f"P{number} 没有写“- 内容来源：…”")
        if not page["lines"]:
            problems.append(f"P{number} 没有上屏文字；{FORMAT_HINT}")
        for line in page["lines"]:
            if line["mark"] == "新增" and not line["reason"]:
                problems.append(f"P{number}“{line['text']}”是新增的字，要写理由：“新增：理由”。原文没有的总结句、口号默认不加")
            if line["mark"] == "引语" and knowledge and not line["reason"]:
                problems.append(f"P{number}“{line['text']}”是引语。知识型的引语、金句和署名默认不上屏；"
                                "用户要留才写“引语：用户的要求”，否则删掉这一行")
            if line["mark"] in VERBATIM and source and not content_map.found_in(line["text"], *source):
                problems.append(f"P{number}“{line['text']}”标的是{line['mark']}，但原文里找不到这句；"
                                "照原文写，或改标“压缩”")
    summary = None
    if skeleton and not any(not (pages[number]["raw"] or "").strip() for number in numbers):
        alignment = content_map.check_pages(skeleton, as_plan_pages(pages))
        problems += alignment["problems"]
        warnings += alignment["warnings"]
        summary = alignment["summary"]
        titles = [len(content_map.bare(pages[number]["lines"][0]["text"])) for number in numbers
                  if pages[number]["lines"] and content_map.parse_page_source(pages[number]["raw"])["kind"] == "nodes"]
        if titles and max(titles) - min(titles) > TITLE_SPREAD:
            warnings.append(f"内容页标题（每页第一行）从 {min(titles)} 个字到 {max(titles)} 个字。字数差得多，出图后字号会不一样大；"
                            "要用字号锁就现在写齐（最多差 2 个字），免得确认后再改")
    lines = [dict(line, page=number) for number in numbers for line in pages[number]["lines"]]
    counts = Counter(line["mark"] for line in lines)

    def listed(mark):
        return [{"page": line["page"], "text": line["text"], "reason": line["reason"]} for line in lines if line["mark"] == mark]

    return {"ok": not problems, "problems": problems, "warnings": warnings, "pages": len(numbers), "lines": len(lines),
            "marks": {mark: counts.get(mark, 0) for mark in MARKS}, "added": listed("新增"), "quotes": listed("引语"),
            "coverage": summary, "copy": pages}


def compare_plan(copy_pages, plans_text, partial=False):
    """Differences between design-plans.md and the page copy; an empty list means they agree."""
    plan = content_map.plan_pages(plans_text)
    copy = as_plan_pages(copy_pages)
    differences = []
    for number in sorted(set(plan) | set(copy)):
        if number not in plan:
            if not partial:
                differences.append(f"P{number} 在文案稿里有，方案里没有")
            continue
        if number not in copy:
            differences.append(f"P{number} 在方案里有，文案稿里没有")
            continue
        ours, theirs = copy[number], plan[number]
        if any(ours[key] != theirs[key] for key in ("kind", "section", "nodes")):
            differences.append(f"P{number} 的 [内容来源] 是“{theirs['raw']}”，文案稿里是“{ours['raw']}”")
        want, got = Counter(ours["copy"]), Counter(theirs["copy"])
        for text in sorted((got - want).elements()):
            differences.append(f"P{number} 方案里多了没确认过的字：“{text}”")
        for text in sorted((want - got).elements()):
            differences.append(f"P{number} 方案里少了已确认的字：“{text}”")
    return differences


def summary_line(report):
    marks = report["marks"]
    return (f"{report['pages']} 页 {report['lines']} 行上屏文字：原文 {marks['原文']}、压缩 {marks['压缩']}、"
            f"新增 {marks['新增']}、引语 {marks['引语']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["check"])
    parser.add_argument("--copy", default="page-copy.md")
    parser.add_argument("--map", default="content-map.md")
    parser.add_argument("--state", help="deck-state.json: source files and the confirmed page count")
    parser.add_argument("--plans", help="design-plans.md: also check that every page shows exactly these lines")
    args = parser.parse_args(argv)
    page_count = None
    if args.state:
        try:
            page_count = json.loads(Path(args.state).read_text(encoding="utf-8"))["workflow"]["brief"].get("page_count")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            page_count = None
    report = check_copy(args.copy, args.map, args.state, page_count)
    pages = report.pop("copy", None)
    ok = report["ok"]
    if args.plans and pages is not None:
        try:
            report["plan_differences"] = compare_plan(pages, Path(args.plans).read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            print(f"page_copy failed: {exc}", file=sys.stderr)
            return 2
        ok = ok and not report["plan_differences"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
