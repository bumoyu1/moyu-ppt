#!/usr/bin/env python3
"""Content skeleton: understand the material before any page exists.

A deck goes wrong when pages are laid out first and the source is mined for
catchy lines afterwards: sections are dropped silently, content lands under
the wrong chapter, and a page shows a slogan instead of what the section says.
The skeleton (`content-map.md`) is written from the material itself:

    - 类型：知识型            (知识型 | 演讲型: what the screen has to carry)
    - 原文：source/讲义.md     (text files, relative to this file; 无 = no source)
    - 主线：新手要从……      (what the speaker argues from start to end)

    ### B 豆子与器具        (section: letter id + the source's own title)
    #### B4 区分风味缺陷与冲煮参数
    - 讲者要讲什么：……          (the point, faithful to the source)
    - 靠什么讲清楚：……          (definition, steps, contrast, data, example)
    - 原文依据：“……”“……”      (verbatim sentences from the source)
    - 待确认：……               (optional: an inference, not the source's words)
    - 不上屏：……               (optional: off-screen, with the reason)

Each page of design-plans.md then names where its content comes from:

    **[内容来源]**：封面 | 目录 | 结尾 | 章节页 B | 回顾 B | 回顾 全篇 | B4 | C3+C4
    **[内容来源]**：B4；移动：结尾需要回扣这一句

    content_map.py check --map content-map.md --state deck-state.json \
        [--plans design-plans.md --coverage coverage.md]

`check` verifies the skeleton (every quote must be found verbatim in the
source) and, with --plans, the alignment: every page has a source, every
section is either on a page or listed off-screen with a reason, pages stay in
their chapter and in source order, and 目录/章节页 carry the chapter titles.
It checks structure and quotations only; whether a point is well understood
is for the user to judge from the skeleton and the coverage table.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

TYPES = ("知识型", "演讲型")
HEADER_RE = re.compile(r"^[ \t]*[-*][ \t]*(类型|原文|主线)[ \t]*[：:][ \t]*(.*?)[ \t]*$")
SECTION_RE = re.compile(r"^###[ \t]+([A-Z]+)[ \t]+(.+?)[ \t]*$")
NODE_RE = re.compile(r"^####[ \t]+([A-Z]+)(\d+)[ \t]+(.+?)[ \t]*$")
FIELD_RE = re.compile(r"^[ \t]*[-*][ \t]*(讲者要讲什么|靠什么讲清楚|原文依据|类型|待确认|不上屏)[ \t]*[：:][ \t]*(.*?)[ \t]*$")
QUOTE_RE = re.compile(r"“((?:[^“”]|“[^“”]*”)*)”")
PAGE_RE = re.compile(r"^#{2,4}\s*第\s*(\d+)\s*页设计方案", re.M)
SOURCE_FIELD_RE = re.compile(r"\*\*\[内容来源\]\*\*[ \t]*[:：][ \t]*([^\r\n]*)")
COPY_RE = re.compile(r"^[ \t]*可见文字[ \t]*=[ \t]*([^；;\r\n]*)", re.M)
NODE_LIST_RE = re.compile(r"^[A-Z]+\d+(?:[ \t]*\+[ \t]*[A-Z]+\d+)*$")
# PDF text extraction leaves radical-block look-alikes (⺫ for 目, ⻆ for 角). The Kangxi
# block folds under NFKC; the supplement block has no reliable mapping, so on the source
# side such a character matches any single character of the quotation.
RADICAL_SUPPLEMENT = "⺀-⻿"
PUNCTUATION = re.compile(r"[\s：:，,、。．.·•\-—–\"'“”‘’（）()《》<>！!？?；;]+")
THIN_PAGE_COPY = 2  # 知识型: a content page with only this many visible strings is a slogan page


def normalize(text):
    text = unicodedata.normalize("NFKC", text)
    text = text.translate({ord("“"): '"', ord("”"): '"', ord("‘"): "'", ord("’"): "'"})
    return re.sub(r"\s+", "", text)


def bare(text):
    """Title comparison ignores spacing and punctuation, nothing else."""
    return PUNCTUATION.sub("", unicodedata.normalize("NFKC", text))


def found_in(quote, source, has_radicals):
    quote = normalize(quote)
    if not quote:
        return False
    if quote in source:
        return True
    if not has_radicals:
        return False
    pattern = "".join(f"(?:{re.escape(char)}|[{RADICAL_SUPPLEMENT}])" for char in quote)
    return re.search(pattern, source) is not None


# ---------------------------------------------------------------- skeleton

def parse_map(text):
    """Return (skeleton, problems). The skeleton keeps source order."""
    skeleton = {"type": None, "sources": None, "throughline": None, "sections": [], "nodes": []}
    problems = []
    section = node = None
    seen = set()
    for number, line in enumerate(text.splitlines(), 1):
        match = SECTION_RE.match(line)
        if match:
            ident, title = match.groups()
            if any(item["id"] == ident for item in skeleton["sections"]):
                problems.append(f"第 {number} 行：部分 {ident} 重复")
            section = {"id": ident, "title": title, "nodes": []}
            skeleton["sections"].append(section)
            node = None
            continue
        match = NODE_RE.match(line)
        if match:
            prefix, digits, title = match.groups()
            ident = prefix + digits
            if section is None or prefix != section["id"]:
                problems.append(f"第 {number} 行：{ident} 要写在“### {prefix} …”这一部分下面")
            if ident in seen:
                problems.append(f"第 {number} 行：{ident} 重复")
            seen.add(ident)
            node = {"id": ident, "section": prefix, "title": title, "fields": {}, "line": number}
            skeleton["nodes"].append(node)
            if section is not None and prefix == section["id"]:
                section["nodes"].append(ident)
            continue
        if line.startswith("#"):
            node = None
            if line.startswith("## ") or line.startswith("# "):
                section = None
            continue
        if node is not None:
            match = FIELD_RE.match(line)
            if match:
                node["fields"][match.group(1)] = match.group(2)
            continue
        if section is None and not skeleton["nodes"]:
            match = HEADER_RE.match(line)
            if match:
                key = {"类型": "type", "原文": "sources", "主线": "throughline"}[match.group(1)]
                value = match.group(2)
                if key == "type":  # "知识型（培训课件）" still means 知识型
                    value = next((name for name in TYPES if value.startswith(name)), value)
                skeleton[key] = value
    if skeleton["type"] not in TYPES:
        problems.append("开头要有一行“- 类型：知识型”或“- 类型：演讲型”")
    if not (skeleton["throughline"] or "").strip():
        problems.append("开头要有一行“- 主线：……”，一两句话说清讲者从头到尾在讲什么")
    if not skeleton["nodes"]:
        problems.append("没有内容节点；每一节写成“#### B4 小节名”加三项说明")
    for node in skeleton["nodes"]:
        fields = node["fields"]
        for name in ("讲者要讲什么", "靠什么讲清楚"):
            if not fields.get(name, "").strip():
                problems.append(f"{node['id']} 缺“{name}”")
        if "不上屏" in fields and not fields["不上屏"].strip():
            problems.append(f"{node['id']} 标了不上屏但没写理由")
        node["off_screen"] = fields.get("不上屏", "").strip() or None
        node["quotes"] = QUOTE_RE.findall(fields.get("原文依据", ""))
    return skeleton, problems


def source_paths(skeleton, map_path, state_path=None):
    """Text files the quotations are checked against. None means the map says 无."""
    declared = skeleton.get("sources")
    base = Path(map_path).resolve().parent
    if declared is not None:
        if declared.strip() in ("无", "无原文", ""):
            return None if declared.strip() else []
        return [base / part.strip() for part in re.split(r"[；;]", declared) if part.strip()]
    if state_path:
        try:
            state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        root = Path(state_path).resolve().parent
        return [root / item for item in state.get("source_files") or [] if isinstance(item, str) and item.strip()]
    return []


def source_text(skeleton, map_path, state_path=None):
    """(normalized source, has_radicals) for verbatim checks; None when no source can be read."""
    texts = []
    for path in source_paths(skeleton, map_path, state_path) or []:
        try:
            texts.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            pass
    if not texts:
        return None
    source = normalize("\n".join(texts))
    return source, re.search(f"[{RADICAL_SUPPLEMENT}]", source) is not None


def check_map(map_path, state_path=None):
    """Validate the skeleton file and its quotations. Returns a report with `problems`."""
    map_path = Path(map_path)
    try:
        skeleton, problems = parse_map(map_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        return {"ok": False, "problems": [f"读不到内容骨架 {map_path}（{type(exc).__name__}）"], "skeleton": None}
    report = {"type": skeleton["type"], "sections": len(skeleton["sections"]), "nodes": len(skeleton["nodes"]),
              "off_screen": [node["id"] for node in skeleton["nodes"] if node["off_screen"]],
              "pending": [node["id"] for node in skeleton["nodes"] if node["fields"].get("待确认", "").strip()],
              "evidence": {"quotes": 0, "not_found": [], "checked": False}}
    paths = source_paths(skeleton, map_path, state_path)
    if paths is None:
        report["evidence"]["note"] = "骨架写明没有原文；提炼是否贴合只能由用户判断"
    else:
        texts = []
        for path in paths:
            try:
                texts.append(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                problems.append(f"原文文件 {path.name} 读不成文本；把素材的文字另存为 .md 或 .txt，写在“- 原文：”这一行")
        if not paths:
            problems.append("没有可核对的原文；在开头写“- 原文：文件路径”，确实没有素材时写“- 原文：无”")
        source = normalize("\n".join(texts))
        has_radicals = re.search(f"[{RADICAL_SUPPLEMENT}]", source) is not None
        for node in skeleton["nodes"]:
            if not node["quotes"]:
                problems.append(f"{node['id']} 没有原文依据；用“……”引出原文里的原句")
            for quote in node["quotes"]:
                report["evidence"]["quotes"] += 1
                if texts and not found_in(quote, source, has_radicals):
                    report["evidence"]["not_found"].append({"node": node["id"], "quote": quote})
        report["evidence"]["checked"] = bool(texts)
        for miss in report["evidence"]["not_found"]:
            problems.append(f"{miss['node']} 的依据在原文里找不到：“{miss['quote']}”。依据必须是原文的原句；自己的概括写在“讲者要讲什么”里")
    report.update(ok=not problems, problems=problems, skeleton=skeleton)
    return report


# ---------------------------------------------------------------- alignment

def plan_pages(text):
    """{page number: {raw, kind, section, nodes, moved, copy}} from design-plans.md."""
    matches = list(PAGE_RE.finditer(text))
    pages = {}
    for index, match in enumerate(matches):
        body = text[match.start():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        field = SOURCE_FIELD_RE.search(body.split("**[空间定位图]**", 1)[0])
        raw = field.group(1).strip() if field else ""
        pages[int(match.group(1))] = {"raw": raw, "copy": [value.strip() for value in COPY_RE.findall(body)],
                                      **parse_page_source(raw)}
    return pages


def parse_page_source(raw):
    parts = [part.strip() for part in re.split(r"[；;]", raw) if part.strip()]
    result = {"kind": None, "section": None, "nodes": [], "moved": False}
    if not parts:
        return result
    head = parts[0]
    moves = [part for part in parts[1:] if part.startswith("移动")]
    result["moved"] = any(re.sub(r"^移动[：: \t]*", "", part).strip() for part in moves)
    if moves and not result["moved"]:
        result["kind"] = "invalid"
        return result
    if head in ("封面", "目录", "结尾"):
        result["kind"] = {"封面": "cover", "目录": "toc", "结尾": "ending"}[head]
    elif re.fullmatch(r"章节页[ \t]*[A-Z]+", head):
        result.update(kind="divider", section=re.sub(r"^章节页[ \t]*", "", head))
    elif re.fullmatch(r"回顾[ \t]*(?:全篇|[A-Z]+)", head):
        target = re.sub(r"^回顾[ \t]*", "", head)
        result.update(kind="recap", section=None if target == "全篇" else target)
    elif NODE_LIST_RE.fullmatch(head):
        result.update(kind="nodes", nodes=[item.strip() for item in head.split("+")])
    else:
        result["kind"] = "invalid"
    return result


def check_plan(skeleton, plans_text):
    """Alignment of design-plans.md to the skeleton."""
    return check_pages(skeleton, plan_pages(plans_text))


def check_pages(skeleton, pages):
    """Alignment of pages ({number: {raw, kind, section, nodes, moved, copy}}) to the skeleton.
    Returns problems, warnings and coverage rows. page_copy.py runs the same rules on the
    page copy, before any design exists."""
    problems, warnings = [], []
    order = {node["id"]: index for index, node in enumerate(skeleton["nodes"])}
    by_id = {node["id"]: node for node in skeleton["nodes"]}
    sections = {section["id"]: section for section in skeleton["sections"]}
    used = {}
    for number in sorted(pages):
        page = pages[number]
        if not page["raw"]:
            problems.append(f"P{number} 没有写 [内容来源]")
            continue
        if page["kind"] == "invalid":
            problems.append(f"P{number} 的 [内容来源]“{page['raw']}”看不懂；写 封面/目录/结尾/章节页 B/回顾 B/回顾 全篇，"
                            "或节点编号（同一部分内可用 + 合并）；调整顺序时加“；移动：理由”")
            continue
        if page["section"] and page["section"] not in sections:
            problems.append(f"P{number} 指向的部分 {page['section']} 不在内容骨架里")
            page["section"] = None
        unknown = [ident for ident in page["nodes"] if ident not in by_id]
        if unknown:
            problems.append(f"P{number} 指向的节点 {'、'.join(unknown)} 不在内容骨架里")
            page["nodes"] = [ident for ident in page["nodes"] if ident in by_id]
        if page["nodes"]:
            owners = {by_id[ident]["section"] for ident in page["nodes"]}
            if len(owners) > 1:
                problems.append(f"P{number} 把不同部分的节点并在一页（{'+'.join(page['nodes'])}）；一页只讲同一部分里的内容")
            page["section"] = by_id[page["nodes"][0]]["section"]
            for ident in page["nodes"]:
                used.setdefault(ident, []).append(number)
    # coverage: every node is on a page or explicitly off-screen
    for node in skeleton["nodes"]:
        if node["id"] in used and node["off_screen"]:
            problems.append(f"{node['id']} 标了不上屏，却出现在 P{'、P'.join(map(str, used[node['id']]))}；二选一")
        elif node["id"] not in used and not node["off_screen"]:
            problems.append(f"{node['id']}《{node['title']}》既没有上屏，也没有写“不上屏：理由”；不能悄悄舍掉")
    # chapters stay together, behind their divider, in source order
    runs = []
    for number in sorted(pages):
        tag = pages[number]["section"]
        if pages[number]["kind"] == "recap" or tag is None:
            continue
        if runs and runs[-1][0] == tag:
            runs[-1][1].append(number)
        else:
            runs.append((tag, [number]))
    seen = {}
    for tag, numbers in runs:
        if tag in seen:
            problems.append(f"部分 {tag}《{sections[tag]['title']}》的页不连续：P{'、P'.join(map(str, seen[tag]))} 和 "
                            f"P{'、P'.join(map(str, numbers))} 之间夹着别的部分")
        seen.setdefault(tag, []).extend(numbers)
    for number in sorted(pages):
        page = pages[number]
        if page["kind"] == "divider" and seen.get(page["section"]) and seen[page["section"]][0] != number:
            problems.append(f"P{number} 是部分 {page['section']} 的章节页，但 P{seen[page['section']][0]} 已经先讲了这一部分")
    last, last_page = -1, None
    for number in sorted(pages):
        page = pages[number]
        if page["kind"] != "nodes" or not page["nodes"]:
            continue
        indices = [order[ident] for ident in page["nodes"]]
        if min(indices) < last and not page["moved"]:
            problems.append(f"P{number}（{'+'.join(page['nodes'])}）排在 P{last_page} 之后，和原文顺序相反；"
                            "按原文顺序排，确实要调整就在 [内容来源] 后写“；移动：理由”")
        if not page["moved"]:
            last, last_page = max(last, max(indices)), number
    # 目录 and 章节页 carry the source's own chapter titles
    with_divider = [page["section"] for page in pages.values() if page["kind"] == "divider" and page["section"]]
    for number in sorted(pages):
        page = pages[number]
        shown = bare("".join(page["copy"]))
        if page["kind"] == "divider" and page["section"] and bare(sections[page["section"]]["title"]) not in shown:
            problems.append(f"P{number} 章节页上的字和这一部分的原题《{sections[page['section']]['title']}》不一致")
        if page["kind"] == "toc":
            for ident in with_divider:
                if bare(sections[ident]["title"]) not in shown:
                    problems.append(f"P{number} 目录里找不到《{sections[ident]['title']}》；目录和章节页都用原文的章名")
    if skeleton["type"] == "知识型":
        for number in sorted(pages):
            page = pages[number]
            if page["kind"] == "nodes" and len(page["copy"]) <= THIN_PAGE_COPY:
                warnings.append(f"P{number}（{'+'.join(page['nodes'])}）只有 {len(page['copy'])} 处文字。知识型的内容页要把这一节"
                                "“靠什么讲清楚”的内容放上屏（框架、步骤或标准），不能只剩标题和一句话")
    pending = [node["id"] for node in skeleton["nodes"] if node["id"] in used and node["fields"].get("待确认", "").strip()]
    if pending:
        warnings.append(f"{'、'.join(pending)} 含有待确认的推断；上屏前请用户确认，不要当成原文的意思")
    rows = []
    for node in skeleton["nodes"]:
        if node["id"] in used:
            numbers = used[node["id"]]
            partners = sorted({other for number in numbers for other in pages[number]["nodes"] if other != node["id"]},
                              key=order.get)
            status = "上屏" + (f"（与 {'、'.join(partners)} 合并）" if partners else "")
            rows.append({"node": node["id"], "title": node["title"], "pages": numbers, "status": status})
        else:
            rows.append({"node": node["id"], "title": node["title"], "pages": [],
                         "status": "不上屏：" + (node["off_screen"] or "（未说明）")})
    return {"ok": not problems, "problems": problems, "warnings": warnings, "coverage": rows,
            "summary": {"nodes": len(rows), "on_screen": sum(1 for row in rows if row["pages"]),
                        "merged": sum(1 for row in rows if "合并" in row["status"]),
                        "off_screen": sum(1 for row in rows if not row["pages"]),
                        "pages": len(pages), "content_pages": sum(1 for page in pages.values() if page["kind"] == "nodes")}}


def coverage_table(skeleton, alignment):
    """The table shown to the user: source structure on the left, pages on the right."""
    summary = alignment["summary"]
    lines = ["# 覆盖表：原文结构 → 页面", "",
             f"类型：{skeleton['type']}。主线：{skeleton['throughline']}", "",
             f"原文共 {summary['nodes']} 节：上屏 {summary['on_screen']} 节（其中 {summary['merged']} 节与同级合并），"
             f"不上屏 {summary['off_screen']} 节。全篇 {summary['pages']} 页，内容页 {summary['content_pages']} 页。", ""]
    titles = {section["id"]: section["title"] for section in skeleton["sections"]}
    current = None
    by_node = {row["node"]: row for row in alignment["coverage"]}
    for node in skeleton["nodes"]:
        if node["section"] != current:
            current = node["section"]
            lines += ["", f"## {current} {titles.get(current, '')}", "", "| 原文小节 | 页 | 处理 |", "|---|---|---|"]
        row = by_node[node["id"]]
        pages = "、".join(f"P{number}" for number in row["pages"]) or "—"
        lines.append(f"| {node['id']} {node['title']} | {pages} | {row['status']} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["check"])
    parser.add_argument("--map", default="content-map.md")
    parser.add_argument("--state", help="deck-state.json, used for source files when the map has no 原文 line")
    parser.add_argument("--plans", help="design-plans.md: also check every page against the skeleton")
    parser.add_argument("--coverage", help="Write the coverage table (Markdown) here; needs --plans")
    args = parser.parse_args(argv)
    report = check_map(args.map, args.state)
    skeleton = report.pop("skeleton")
    output = {"map": report}
    ok = report["ok"]
    if args.plans and skeleton is not None:
        try:
            alignment = check_plan(skeleton, Path(args.plans).read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            print(f"content_map failed: {exc}", file=sys.stderr)
            return 2
        output["plan"] = alignment
        ok = ok and alignment["ok"]
        if args.coverage:
            Path(args.coverage).parent.mkdir(parents=True, exist_ok=True)
            Path(args.coverage).write_text(coverage_table(skeleton, alignment), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
