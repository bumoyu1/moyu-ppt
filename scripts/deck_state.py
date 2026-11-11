#!/usr/bin/env python3
"""Record real deck decisions in deck-state.json and show what the next gate needs.

The agent passes the user's actual words; this tool adds timezone timestamps,
keeps workflow as the single decision source, and moves a confirmation into
history when what it confirmed changes. It never invents a decision, reads keys
or uses the network. workflow_gate.py remains the read-only checker.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from deck_runtime import atomic_write, json_bytes, validate_image_file
from workflow_gate import BARE_DEFAULTS, STAGES, check_workflow, positive_integer, ratio
import content_map
import page_copy
import visual_budget

SKILL_DIR = Path(__file__).resolve().parents[1]
OUTPUTS = ("inspiration", "style", "plans", "deck", "revise", "template")
DECISIONS = ("intake", "copy", "visual", "pilot")
BRIEF_FIELDS = ("page_count", "aspect_ratio", "audience", "purpose", "visual_ratio")
# Deck-wide image/text budget ("visual/text"). Unrecorded means the documented
# default, so a later non-default value counts as a change to a confirmed brief.
DEFAULT_VISUAL_RATIO = "80/20"
PER_PAGE = "per-page"
# How content-page titles are laid out: one position and size on every page, or wherever each
# page's composition puts them. A deck without the field was built on the uniform layout.
TITLE_LAYOUTS = {"uniform": "统一版式", "free": "自由构图"}
DEFAULT_TITLE_LAYOUT = "uniform"
TEXT_ARTIFACTS = ("content_map", "page_copy", "visual_proposal", "inspiration_review", "style_config", "pilot_review")
IMAGE_ARTIFACTS = ("typography_specimen", "pilot_image")
TEMPLATE_STATUSES = ("offered", "declined", "approved", "saved")
LABELS = {"intake": "需求", "copy": "文案稿", "visual": "视觉方向与字体", "pilot": "样页"}
# Only full plans and decks pass the deck gates; other scopes end earlier.
GATED_STAGES = {"plans": ("plans",), "deck": STAGES}
STAGE_LABELS = {"plans": "拆分全篇方案", "pilot": "生成样页", "production": "生成余页"}
# Which confirmations depend on which recorded inputs.
DEPENDENTS = {"brief": ("intake", "copy", "pilot"), "content_map": ("intake", "copy", "pilot"),
              "page_copy": ("copy", "pilot"), "typography": ("visual", "pilot"),
              "style_config": ("visual", "pilot"), "typography_specimen": ("pilot",),
              "pilot_pages": ("pilot",), "pilot_image": ("pilot",)}


class StateError(ValueError):
    pass


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def skill_version():
    try:
        return json.loads((SKILL_DIR / "manifest.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def visual_ratio(value):
    """Normalize the image/text budget: 'V/T' with V+T=100, or per-page."""
    text = value.strip().lower() if isinstance(value, str) else ""
    if text == PER_PAGE:
        return PER_PAGE
    match = re.fullmatch(r"(\d{1,2})\s*/\s*(\d{1,2})", text)
    if match:
        visual, words = (int(part) for part in match.groups())
        if visual > 0 and words > 0 and visual + words == 100:
            return f"{visual}/{words}"
    raise StateError("visual_ratio 写成“视觉/文字”且两数之和为 100（如 80/20、60/40、50/50），"
                     "或 per-page（用户要求按内容逐页定）。")


def title_layout(value):
    """Normalize the title layout to uniform or free; the Chinese names are accepted too."""
    text = value.strip().lower() if isinstance(value, str) else ""
    names = {**{key: key for key in TITLE_LAYOUTS}, **{label: key for key, label in TITLE_LAYOUTS.items()}}
    if text in names:
        return names[text]
    raise StateError("title_layout 写 uniform（统一版式：内容页标题每页同一位置、同一大小）"
                     "或 free（自由构图：标题的位置和排法跟着每页的画面走）。")


def load(path):
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise StateError(f"{path} 不存在；新任务先运行 init。") from None
    except ValueError as exc:
        raise StateError(f"{path} 不是有效 JSON：{exc}") from None
    if not isinstance(state, dict) or not isinstance(state.get("workflow"), dict):
        raise StateError(f"{path} 缺 workflow 对象；运行 init --adopt 补骨架，再从真实对话重新记录决定（旧 pace/completed 不算确认）。")
    return state


def save(path, state):
    atomic_write(path, json_bytes(state))


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def relative_to_state(state_path, value):
    """Store artifact paths relative to deck-state.json, as workflow_gate resolves them."""
    target = Path(value)
    if not target.is_absolute():
        target = Path.cwd() / target
    target = target.resolve()
    base = Path(state_path).resolve().parent
    try:
        return target, os.path.relpath(target, base) if os.path.commonpath([target, base]) == str(base) else str(target)
    except ValueError:  # different drives on Windows
        return target, str(target)


def invalidate(workflow, cause, reason):
    """Move confirmations that depended on `cause` into history and report them."""
    decisions = workflow.setdefault("decisions", {})
    history = workflow.setdefault("decision_history", [])
    dropped = []
    for name in DEPENDENTS[cause]:
        record = decisions.pop(name, None)
        if record:
            history.append({**record, "decision": name, "status": "invalidated",
                            "invalidated_at": now(), "invalidated_reason": reason})
            dropped.append(name)
    return dropped


def report_invalidated(dropped):
    if dropped:
        names = "、".join(LABELS[name] for name in dropped)
        print(f"注意：已确认内容发生变化，{names}确认移入历史；需要重新向用户展示并取得确认。", file=sys.stderr)


# ---------------------------------------------------------------- commands

def new_workflow():
    return {
        "schema_version": 1,
        "mode": "standard",
        # Decks read the material into a content skeleton first (content_map.py);
        # a state without the flag is not blocked by it.
        "content_map_required": True,
        # ...then write every on-screen line into one copy sheet the user confirms before any design (page_copy.py).
        "page_copy_required": True,
        # ...and keep most pages at the confirmed image/text ratio, none below the floor (visual_budget.py).
        "visual_budget_rules": True,
        # ...and say how each page connects to its neighbours; 变奏/亮点 pages write their own composition (continuity.py).
        "continuity_rules": True,
        # ...and have the user choose how content-page titles are laid out: uniform or free.
        "title_layout_rules": True,
        "brief": {"page_count": None, "aspect_ratio": None, "audience": "", "purpose": "", "visual_ratio": None,
                  "title_layout": None},
        "proposals": {},
        "decisions": {},
        "artifacts": {},
        "typography_decision": {},
        "pilot_pages": [],
    }


def runtime_record():
    return {"path": str(SKILL_DIR), "version": skill_version(), "read_at": now()}


def cmd_init(args):
    path = Path(args.state)
    if args.adopt:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise StateError(f"{path} 不存在；--adopt 只用于已有的旧状态。") from None
        except ValueError as exc:
            raise StateError(f"{path} 不是有效 JSON：{exc}") from None
        if not isinstance(state, dict):
            raise StateError(f"{path} 顶层不是对象。")
        if isinstance(state.get("workflow"), dict):
            raise StateError(f"{path} 已有 workflow；直接用 status。")
        if args.deck_name:
            state["deck_name"] = args.deck_name
        if args.requested_output:
            state["requested_output"] = args.requested_output
        if state.get("requested_output") not in OUTPUTS:
            raise StateError("旧状态没有有效的 requested_output；用 --requested-output 指定。")
        state["workflow"] = new_workflow()
        state.setdefault("template_offer", {"status": "not_offered"})
        state.setdefault("assumptions", [])
        state["skill_runtime"] = runtime_record()
        state["adopted_at"] = now()
        save(path, state)
        print(f"已给旧状态补 workflow 骨架（standard）。旧 pace/completed 不算确认，请从实际对话重新记录决定。")
        return
    if not (args.deck_name and args.requested_output):
        raise StateError("新建状态需要 --deck-name 与 --requested-output。")
    if path.exists():
        raise StateError(f"{path} 已存在；恢复任务用 status，旧状态缺 workflow 时用 init --adopt。")
    state = {
        "schema_version": 1,
        "deck_name": args.deck_name,
        "requested_output": args.requested_output,
        "skill_runtime": runtime_record(),
        "source_files": args.source or [],
        "assumptions": [],
        "capabilities": {},
        "workflow": new_workflow(),
        "template_offer": {"status": "not_offered"},
    }
    save(path, state)
    print(f"已创建 {path}（standard 模式，范围 {args.requested_output}）。")


def parse_brief_values(args):
    values = {}
    if args.page_count is not None:
        if not positive_integer(args.page_count):
            raise StateError("page_count 必须是正整数。")
        values["page_count"] = args.page_count
    if args.aspect_ratio is not None:
        ratio(args.aspect_ratio)
        values["aspect_ratio"] = args.aspect_ratio
    for field in ("audience", "purpose"):
        value = getattr(args, field)
        if value is not None:
            if not nonempty(value):
                raise StateError(f"{field} 不能为空。")
            values[field] = value.strip()
    if args.visual_ratio is not None:
        values["visual_ratio"] = visual_ratio(args.visual_ratio)
    if getattr(args, "title_layout", None) is not None:
        values["title_layout"] = title_layout(args.title_layout)
    if getattr(args, "visual_floor", None) is not None:
        if not 0 < args.visual_floor < 100:
            raise StateError("visual_floor 是画面占比的下限，写 1–99 的整数，如 80 表示最低 80/20。")
        values["visual_floor"] = args.visual_floor
    if not values:
        raise StateError("至少提供 --page-count/--aspect-ratio/--audience/--purpose/--visual-ratio/--visual-floor/--title-layout 之一。")
    return values


def recorded(brief, key):
    """The value a confirmation relied on; an unrecorded ratio or floor meant the default."""
    value = brief.get(key)
    if key == "visual_ratio" and value in (None, ""):
        return DEFAULT_VISUAL_RATIO
    if key == "title_layout" and value in (None, ""):
        return DEFAULT_TITLE_LAYOUT
    if key == "visual_floor" and value is None:
        limits = visual_budget.base_and_floor({"visual_ratio": recorded(brief, "visual_ratio")})
        return limits[1] if limits else None
    return value


def merged_brief(brief, values):
    """Brief after an update. A new ratio without a new floor drops an explicit floor, so the
    floor goes back to 5 points below the new base instead of silently staying where it was."""
    merged = {**brief, **values}
    if "visual_ratio" in values and "visual_floor" not in values and brief.get("visual_floor") is not None \
            and recorded(brief, "visual_ratio") != values["visual_ratio"]:
        merged.pop("visual_floor")
    floor = merged.get("visual_floor")
    if floor is not None:
        base = visual_budget.parse_ratio(str(recorded(merged, "visual_ratio")))
        if not base:
            raise StateError("per-page 不设下限；下限只配整体占比，如 --visual-ratio 85/15 --visual-floor 80。")
        if floor > base[0]:
            raise StateError(f"下限 {floor}/{100 - floor} 高于基准 {base[0]}/{base[1]}；下限写画面占比，不能高于基准。")
    return merged


def apply_brief(workflow, values, reason):
    brief = workflow.setdefault("brief", {})
    merged = merged_brief(brief, values)
    changed = [key for key, value in values.items()
               if recorded(brief, key) not in (None, "") and recorded(brief, key) != value]
    brief.clear()
    brief.update(merged)
    return invalidate(workflow, "brief", reason) if changed else []


def cmd_brief(args):
    state = load(args.state)
    workflow = state["workflow"]
    values = parse_brief_values(args)
    if args.proposed:
        workflow.setdefault("proposals", {}).setdefault("brief", {}).update(values)
        save(args.state, state)
        print("已记录为待确认建议（proposals.brief），未写入已确认简报。")
        return
    explicit_floor = workflow.get("brief", {}).get("visual_floor")
    dropped = apply_brief(workflow, values, "brief changed: " + ", ".join(sorted(values)))
    save(args.state, state)
    print("已写入简报：" + "，".join(f"{k}={v}" for k, v in values.items()))
    if explicit_floor is not None and "visual_floor" not in workflow["brief"]:
        print(f"图文基准变了，原来单独设的下限 {explicit_floor} 已取消，回到默认：比新基准低 {visual_budget.DEFAULT_FLOOR_GAP} 点。")
    report_invalidated(dropped)


def cmd_decide(args):
    state = load(args.state)
    workflow = state["workflow"]
    if not nonempty(args.quote):
        raise StateError("--quote 必须是用户的实际回复原话。")
    dropped = []
    if args.apply_proposal:
        if args.decision != "intake":
            raise StateError("--apply-proposal 只用于需求确认。")
        proposal = workflow.get("proposals", {}).pop("brief", None)
        if not proposal:
            raise StateError("没有待确认的 proposals.brief。")
        dropped = apply_brief(workflow, proposal, "brief changed by accepted proposal")
    record = {"status": "confirmed", "source": "user", "evidence": args.quote.strip(),
              "confirmed_at": now()}
    if args.context:
        record["context"] = args.context.strip()
    artifacts = workflow.get("artifacts", {})
    base = Path(args.state).resolve().parent
    if args.decision == "intake":
        # ① shows the content skeleton: type, throughline, per-section points and what stays off-screen.
        skeleton_path = artifacts.get("content_map")
        if nonempty(skeleton_path) and (base / skeleton_path).is_file():
            record["artifacts_sha256"] = {"content_map": file_sha(base / skeleton_path)}
        elif workflow.get("content_map_required") and state.get("requested_output") in GATED_STAGES:
            raise StateError("需求确认前先读完素材、写出内容骨架并给用户看：content_map.py check 通过后用 "
                             "artifact content_map content-map.md 登记。页数建议要从骨架得出，不能先定页数再找内容。")
        brief = workflow.get("brief", {})
        missing = [field for field in BRIEF_FIELDS if brief.get(field) in (None, "")]
        if workflow.get("title_layout_rules") and brief.get("title_layout") in (None, ""):
            missing.append("title_layout")
        if missing:
            hint = ("图文融合占比也属于①的建议组：默认建议视觉主导 80/20，用户可选整体占比或 per-page。"
                    if "visual_ratio" in missing else "")
            if "title_layout" in missing:
                hint += ("标题排版也属于①的建议组：统一版式（内容页标题每页同一位置、同一大小）或自由构图"
                         "（标题跟着每页的画面走），用 brief --title-layout uniform|free 记录。")
            raise StateError("简报缺 " + "、".join(missing) + "；先用 brief 写入用户给出的值，或用 --apply-proposal 接受已展示的建议。" + hint)
        leftover = workflow.get("proposals", {}).pop("brief", None)
        if leftover:  # the user answered with their own values instead
            workflow.setdefault("proposal_history", []).append(
                {"brief": leftover, "status": "superseded_by_user_values", "at": now()})
    elif args.decision == "copy":
        # ② shows one document: every line that will be on a slide, with added lines and quotations marked.
        copy_path = artifacts.get("page_copy")
        if not (nonempty(copy_path) and (base / copy_path).is_file()):
            raise StateError("先把每页要上屏的文字写成文案稿并给用户看：page_copy.py check 通过后用 "
                             "artifact page_copy page-copy.md 登记。")
        report = copy_report(workflow, args.state, base / copy_path)
        if not report["ok"]:
            raise StateError("文案稿还有问题，改好再确认：\n" + "\n".join(f"  - {item}" for item in report["problems"]))
        record["artifacts_sha256"] = {"page_copy": file_sha(base / copy_path)}
    elif args.decision == "visual":
        snapshot = {}
        for name in ("style_config", "typography_specimen"):
            if nonempty(artifacts.get(name)) and (base / artifacts[name]).is_file():
                snapshot[name] = file_sha(base / artifacts[name])
        if snapshot:
            record["artifacts_sha256"] = snapshot
    elif args.decision == "pilot":
        pages = workflow.get("pilot_pages") or []
        images = artifacts.get("pilot_images") or {}
        if not pages:
            raise StateError("先用 pilot --pages 登记样页页码。")
        snapshot = {}
        for page in pages:
            value = images.get(str(page))
            if not nonempty(value):
                raise StateError(f"样页 P{page} 没有登记图片；用户必须先看到实际样页。")
            snapshot[str(page)] = validate_image_file(base / value)["sha256"]
        record["images_sha256"] = snapshot
    decisions = workflow.setdefault("decisions", {})
    if args.decision in decisions:
        workflow.setdefault("decision_history", []).append(
            {**decisions[args.decision], "decision": args.decision, "status": "superseded", "superseded_at": now()})
    decisions[args.decision] = record
    save(args.state, state)
    report_invalidated([name for name in dropped if name != "intake"])
    print(f"已记录用户对{LABELS[args.decision]}的确认。")


def cmd_reopen(args):
    """The user withdrew a confirmation; move it (and what depended on it) into history."""
    state = load(args.state)
    workflow = state["workflow"]
    if not nonempty(args.quote):
        raise StateError("--quote 必须是用户收回该决定的实际原话。")
    decisions = workflow.setdefault("decisions", {})
    history = workflow.setdefault("decision_history", [])
    chain = {"intake": ("intake", "copy", "visual", "pilot"), "copy": ("copy", "pilot"),
             "visual": ("visual", "pilot"), "pilot": ("pilot",)}[args.decision]
    dropped = []
    for name in chain:
        record = decisions.pop(name, None)
        if record:
            history.append({**record, "decision": name, "status": "withdrawn", "invalidated_at": now(),
                            "invalidated_reason": "用户收回：" + args.quote.strip()})
            dropped.append(name)
    save(args.state, state)
    if dropped:
        print("已收回：" + "、".join(LABELS[name] for name in dropped) + "。需要重新展示并取得确认。")
    else:
        print("没有可收回的确认。")


def cmd_delegate(args):
    state = load(args.state)
    workflow = state["workflow"]
    if args.revoke:
        if workflow.get("delegation"):
            workflow.setdefault("decision_history", []).append(
                {**workflow["delegation"], "decision": "delegation", "status": "revoked", "revoked_at": now()})
        workflow.pop("delegation", None)
        workflow["mode"] = "standard"
        save(args.state, state)
        print("已撤销代决策授权，回到 standard。")
        return
    quote = (args.quote or "").strip()
    if not quote:
        raise StateError("--quote 必须是用户授权的原话。")
    if quote.lower().strip("。.!！ ") in BARE_DEFAULTS:
        raise StateError("“默认/好的/继续”只确认上一条展示的内容，不等于全程代决策授权。")
    scopes = [scope.strip() for scope in (args.scopes or "").split(",") if scope.strip()]
    if not scopes or any(scope not in DECISIONS for scope in scopes) or len(scopes) != len(set(scopes)):
        raise StateError("--scopes 取 intake,copy,visual,pilot 中用户实际授权的部分，以逗号分隔。")
    workflow["mode"] = "autonomous"
    workflow["delegation"] = {"source": "user", "evidence": quote, "scopes": scopes, "confirmed_at": now()}
    save(args.state, state)
    print("已记录代决策授权，范围：" + "、".join(LABELS[s] for s in scopes) + "。样页、自检和字体产物仍需完成。")


def cmd_typography(args):
    state = load(args.state)
    workflow = state["workflow"]
    if not (nonempty(args.reason) and nonempty(args.summary)):
        raise StateError("--reason 与 --summary 不能为空；summary 写已向用户展示的字体方案。")
    new = {"specimen": args.specimen, "reason": args.reason.strip(), "summary": args.summary.strip()}
    old = workflow.get("typography_decision") or {}
    workflow["typography_decision"] = new
    dropped = invalidate(workflow, "typography", "typography decision changed") if old and old != new else []
    save(args.state, state)
    print(f"已记录字体方案：specimen={args.specimen}。")
    report_invalidated(dropped)


def cmd_artifact(args):
    state = load(args.state)
    workflow = state["workflow"]
    artifacts = workflow.setdefault("artifacts", {})
    target, stored = relative_to_state(args.state, args.path)
    if not target.is_file() or target.stat().st_size == 0:
        raise StateError(f"{args.path} 不存在或为空；只登记已经完成的文件。")
    if args.name in IMAGE_ARTIFACTS:
        validate_image_file(target)
    if args.name == "content_map":
        report = content_map.check_map(target, args.state)
        if not report["ok"]:
            raise StateError("内容骨架还不能登记：\n" + "\n".join(f"  - {item}" for item in report["problems"]))
    notes = []
    if args.name == "page_copy":
        report = copy_report(workflow, args.state, target)
        if not report["ok"]:
            raise StateError("文案稿还不能登记：\n" + "\n".join(f"  - {item}" for item in report["problems"]))
        notes = [page_copy.summary_line(report) + "。新增和引语要单独指给用户看。"] + ["提醒：" + item for item in report["warnings"]]
    if args.name == "pilot_image":
        if not positive_integer(args.page):
            raise StateError("pilot_image 需要 --page 页码。")
        images = artifacts.setdefault("pilot_images", {})
        previous = images.get(str(args.page))
        images[str(args.page)] = stored
        cause = "pilot_image"
    else:
        if args.page is not None:
            raise StateError("--page 只用于 pilot_image。")
        previous = artifacts.get(args.name)
        artifacts[args.name] = stored
        cause = args.name if args.name in DEPENDENTS else None
    dropped = []
    if cause and previous and previous != stored:
        dropped = invalidate(workflow, cause, f"{args.name} path changed")
    save(args.state, state)
    print(f"已登记 {args.name}{'' if args.page is None else ' P' + str(args.page)}：{stored}")
    for note in notes:
        print(note)
    report_invalidated(dropped)


def cmd_pilot(args):
    state = load(args.state)
    workflow = state["workflow"]
    try:
        pages = [int(item) for item in args.pages.split(",") if item.strip()]
    except ValueError:
        raise StateError("--pages 使用逗号分隔的页码，例如 1,4。") from None
    count = workflow.get("brief", {}).get("page_count")
    if not 1 <= len(pages) <= 3 or len(set(pages)) != len(pages) or any(page < 1 for page in pages):
        raise StateError("样页为 1–3 个不同的正整数页码。")
    if positive_integer(count) and any(page > count for page in pages):
        raise StateError(f"样页页码超出已确认的 {count} 页。")
    old = workflow.get("pilot_pages") or []
    workflow["pilot_pages"] = pages
    dropped = invalidate(workflow, "pilot_pages", "pilot pages changed") if old and old != pages else []
    save(args.state, state)
    print("已登记样页：" + "、".join(f"P{page}" for page in pages))
    report_invalidated(dropped)


def cmd_scope(args):
    state = load(args.state)
    state["requested_output"] = args.requested_output
    save(args.state, state)
    print(f"范围改为 {args.requested_output}。")


def cmd_assume(args):
    state = load(args.state)
    state.setdefault("assumptions", []).append({"text": args.text, "recorded_at": now()})
    save(args.state, state)
    print("已记录假设（不是用户确认）。")


def cmd_capabilities(args):
    state = load(args.state)
    caps = state.setdefault("capabilities", {})
    for pair in args.pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise StateError(f"能力项写成 key=value：{pair}")
        caps[key.strip()] = value.strip()
    caps["recorded_at"] = now()
    save(args.state, state)
    print("已记录能力快照。")


def cmd_template_offer(args):
    state = load(args.state)
    offer = state.setdefault("template_offer", {"status": "not_offered"})
    status = offer.get("status", "not_offered")
    quote = (args.quote or "").strip()
    if args.status == "offered":
        if status != "not_offered" and not args.user_requested:
            raise StateError(f"模板收藏已是 {status}；同一套 deck 不重复询问。用户主动要求时加 --user-requested。")
        offer.update({"status": "offered", "offered_at": now()})
        if args.candidate_file:
            offer["candidate_file"] = args.candidate_file
    elif args.status in ("declined", "approved"):
        if not quote:
            raise StateError("--quote 写用户的实际回复。")
        offer.update({"status": args.status, "decision_note": quote, "decided_at": now()})
    else:  # saved
        if not (nonempty(args.template_id) and nonempty(args.version)):
            raise StateError("saved 需要 template_library.py 返回的 --template-id 与 --version。")
        if status != "approved" and not quote:
            raise StateError("保存前需要用户同意：先记录 approved，或用 --quote 记下用户直接要求保存的原话。")
        if quote:
            offer["decision_note"] = quote
        offer.update({"status": "saved", "template_id": args.template_id, "version": args.version, "saved_at": now()})
    save(args.state, state)
    print(f"模板收藏状态：{offer['status']}。")


def drift_warnings(state, base):
    workflow = state["workflow"]
    decisions = workflow.get("decisions", {})
    artifacts = workflow.get("artifacts", {})
    warnings = []
    for name, digest in (decisions.get("visual", {}).get("artifacts_sha256") or {}).items():
        path = base / str(artifacts.get(name, ""))
        if not path.is_file() or file_sha(path) != digest:
            warnings.append(f"视觉确认后 {name} 已改变；重新展示或在用户同意后重新确认。")
    for name, digest in (decisions.get("intake", {}).get("artifacts_sha256") or {}).items():
        path = base / str(artifacts.get(name, ""))
        if not path.is_file() or file_sha(path) != digest:
            warnings.append("需求确认后内容骨架已改变；类型、上屏范围或各节要点有变时重新给用户看并确认。")
    for name, digest in (decisions.get("copy", {}).get("artifacts_sha256") or {}).items():
        path = base / str(artifacts.get(name, ""))
        if not path.is_file() or file_sha(path) != digest:
            warnings.append("文案稿在用户确认后改过；把改动给用户看，重新 decide copy 后才能拆分和生图。")
    for page, digest in (decisions.get("pilot", {}).get("images_sha256") or {}).items():
        path = base / str((artifacts.get("pilot_images") or {}).get(page, ""))
        if not path.is_file() or file_sha(path) != digest:
            warnings.append(f"样页 P{page} 在用户确认后已改变；重新展示后再生产余页。")
    delegated = "intake" in ((workflow.get("delegation") or {}).get("scopes") or [])
    if (decisions.get("intake") or delegated) and (workflow.get("brief") or {}).get("visual_ratio") in (None, ""):
        warnings.append(f"需求已确认但没有记录图文融合占比，按默认 {DEFAULT_VISUAL_RATIO} 设计；用户另有要求时用 "
                        f"brief --visual-ratio 记录，不是 {DEFAULT_VISUAL_RATIO} 会让需求与样页确认失效。")
    runtime = state.get("skill_runtime") or {}
    if runtime.get("version") and skill_version() and runtime["version"] != skill_version():
        warnings.append(f"状态记录的 skill 版本 {runtime['version']} 与当前 {skill_version()} 不同；重读 SKILL.md 与相应 references，旧导出包重新预检。")
    return warnings


def content_summary(workflow, base):
    path = (workflow.get("artifacts") or {}).get("content_map")
    if not nonempty(path):
        return "（未登记）" if workflow.get("content_map_required") else "（旧作品，没有内容骨架）"
    report = content_map.check_map(base / path, base / "deck-state.json")
    if report.get("skeleton") is None or not report["ok"]:
        return "有问题：" + "；".join(report["problems"][:2])
    return (f"{report['type']} · {report['sections']} 个部分 {report['nodes']} 节 · 不上屏 {len(report['off_screen'])} 节"
            + (f" · 待确认 {len(report['pending'])} 处" if report["pending"] else ""))


def copy_report(workflow, state_path, copy_path):
    """Check the copy sheet against the registered skeleton and the confirmed page count."""
    skeleton_path = (workflow.get("artifacts") or {}).get("content_map")
    base = Path(state_path).resolve().parent
    return page_copy.check_copy(copy_path, base / skeleton_path if nonempty(skeleton_path) else None, state_path,
                                (workflow.get("brief") or {}).get("page_count"))


def copy_summary(workflow, state_path):
    path = (workflow.get("artifacts") or {}).get("page_copy")
    if not nonempty(path):
        return "（未登记）" if workflow.get("page_copy_required") else "（这套作品没有这一步）"
    report = copy_report(workflow, state_path, Path(state_path).resolve().parent / path)
    if not report["ok"]:
        return "有问题：" + "；".join(report["problems"][:2])
    return page_copy.summary_line(report)


def summarize(state, state_path):
    base = Path(state_path).resolve().parent
    workflow = state["workflow"]
    stages = GATED_STAGES.get(state.get("requested_output"), ())
    checks = {stage: check_workflow(state_path, stage) for stage in stages}
    next_stage = next((stage for stage in stages if not checks[stage]["ok"]), None)
    decisions = {}
    for name in DECISIONS:
        record = (workflow.get("decisions") or {}).get(name)
        scopes = (workflow.get("delegation") or {}).get("scopes") or []
        if record:
            decisions[name] = f"用户确认 {record.get('confirmed_at', '')}"
        elif workflow.get("mode") == "autonomous" and name in scopes:
            decisions[name] = "代决策授权"
        else:
            decisions[name] = "未确认"
    return {
        "deck_name": state.get("deck_name"),
        "requested_output": state.get("requested_output"),
        "mode": workflow.get("mode"),
        "skill_version": (state.get("skill_runtime") or {}).get("version"),
        "brief": workflow.get("brief"),
        "visual_budget_rules": bool(workflow.get("visual_budget_rules")),
        "title_layout_rules": bool(workflow.get("title_layout_rules")),
        "proposals": workflow.get("proposals") or {},
        "decisions": decisions,
        "typography_decision": workflow.get("typography_decision") or {},
        "content_map": content_summary(workflow, base),
        "page_copy": copy_summary(workflow, state_path),
        "artifacts": workflow.get("artifacts") or {},
        "pilot_pages": workflow.get("pilot_pages") or [],
        "template_offer": state.get("template_offer") or {},
        "gates": {stage: checks[stage]["ok"] for stage in stages},
        "next_stage": next_stage,
        "next_steps": checks[next_stage]["next_steps"] if next_stage else [],
        "warnings": drift_warnings(state, base),
    }


def brief_display(brief, field, budget_rules=False):
    if brief.get(field) in (None, ""):
        return f"（缺，默认 {DEFAULT_VISUAL_RATIO}）" if field == "visual_ratio" else "（缺）"
    limits = visual_budget.base_and_floor(brief) if field == "visual_ratio" and budget_rules else None
    return f"{brief[field]}，下限 {limits[1]}/{100 - limits[1]}" if limits else brief[field]


def cmd_status(args):
    state = load(args.state)
    summary = summarize(state, args.state)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    brief = summary["brief"] or {}
    lines = [f"deck：{summary['deck_name']} · 范围 {summary['requested_output']} · 模式 {summary['mode']} · skill {summary['skill_version']}",
             "简报：" + " · ".join(f"{field}={brief_display(brief, field, summary['visual_budget_rules'])}"
                                  for field in BRIEF_FIELDS)]
    if summary["title_layout_rules"] or brief.get("title_layout"):
        lines[-1] += " · title_layout=" + (TITLE_LAYOUTS.get(brief.get("title_layout")) or "（缺）")
    if summary["proposals"]:
        lines.append("待确认建议：" + json.dumps(summary["proposals"], ensure_ascii=False))
    lines.append("决定：" + " · ".join(f"{LABELS[name]} {value}" for name, value in summary["decisions"].items()))
    typo = summary["typography_decision"]
    lines.append(f"内容骨架：{summary['content_map']}")
    lines.append(f"文案稿：{summary['page_copy']}")
    lines.append(f"字体：{typo.get('specimen', '（未记录）')}" + (f" — {typo['summary']}" if typo.get("summary") else ""))
    lines.append("样页：" + ("、".join(f"P{p}" for p in summary["pilot_pages"]) or "（未登记）"))
    lines.append(f"模板收藏：{summary['template_offer'].get('status', 'not_offered')}")
    if summary["gates"]:
        lines.append("门禁：" + " / ".join(f"{STAGE_LABELS[stage]}({stage}) {'✓' if ok else '✗'}" for stage, ok in summary["gates"].items()))
    else:
        lines.append("门禁：此范围不经过整套生成门禁，按 SKILL.md 的范围完成条件结束。")
    if summary["next_stage"]:
        lines.append(f"{STAGE_LABELS[summary['next_stage']]}（{summary['next_stage']}）前还需要：")
        lines.extend(f"  - {step}" for step in summary["next_steps"])
    elif summary["requested_output"] == "plans":
        lines.append("方案门禁已通过；交付 design-plans.md，不自动生图。")
    elif summary["requested_output"] == "deck":
        lines.append("可以生成余页；全部检查通过后装配 PPTX。")
        if summary["template_offer"].get("status", "not_offered") == "not_offered":
            lines.append("交付并完成检查后，询问一次是否收藏为模板。")
    for warning in summary["warnings"]:
        lines.append("⚠ " + warning)
    print("\n".join(lines))


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", default="deck-state.json", help="deck-state.json path (default: ./deck-state.json)")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", parents=[common], help="Create deck-state.json for a new deck")
    p.add_argument("--deck-name")
    p.add_argument("--requested-output", choices=OUTPUTS)
    p.add_argument("--source", action="append", help="Source material path; repeatable")
    p.add_argument("--adopt", action="store_true", help="Add a workflow skeleton to an old state that lacks one")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("brief", parents=[common], help="Write user-given brief values, or --proposed suggestions")
    p.add_argument("--page-count", type=int)
    p.add_argument("--aspect-ratio")
    p.add_argument("--audience")
    p.add_argument("--purpose")
    p.add_argument("--visual-ratio", help="Deck-wide visual/text budget, e.g. 80/20, 60/40, 50/50, or per-page")
    p.add_argument("--visual-floor", type=int,
                   help="Lowest picture share any page may use (default: 5 below the ratio, e.g. 80 for 85/15)")
    p.add_argument("--title-layout", help="Content-page titles: uniform (same position and size on every page) "
                                           "or free (placed by each page's composition)")
    p.add_argument("--proposed", action="store_true", help="Record as a suggestion awaiting confirmation")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("decide", parents=[common], help="Record the user's actual confirmation")
    p.add_argument("decision", choices=DECISIONS)
    p.add_argument("--quote", required=True, help="The user's actual reply")
    p.add_argument("--context", help="What was shown when the user replied")
    p.add_argument("--apply-proposal", action="store_true", help="intake: the user accepted proposals.brief")
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("reopen", parents=[common], help="The user withdrew a confirmation (and what depends on it)")
    p.add_argument("decision", choices=DECISIONS)
    p.add_argument("--quote", required=True, help="The user's actual words withdrawing it")
    p.set_defaults(func=cmd_reopen)

    p = sub.add_parser("delegate", parents=[common], help="Record explicit delegation (autonomous mode)")
    p.add_argument("--quote", help="The user's actual delegation words")
    p.add_argument("--scopes", help="Comma list from intake,copy,visual,pilot")
    p.add_argument("--revoke", action="store_true")
    p.set_defaults(func=cmd_delegate)

    p = sub.add_parser("typography", parents=[common], help="Record the shown typography plan and specimen choice")
    p.add_argument("--specimen", choices=("generate", "provided", "skip"), required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--summary", required=True, help="Typography plan as shown to the user")
    p.set_defaults(func=cmd_typography)

    p = sub.add_parser("artifact", parents=[common], help="Register a finished file")
    p.add_argument("name", choices=TEXT_ARTIFACTS + IMAGE_ARTIFACTS)
    p.add_argument("path")
    p.add_argument("--page", type=int, help="Page number for pilot_image")
    p.set_defaults(func=cmd_artifact)

    p = sub.add_parser("pilot", parents=[common], help="Register 1-3 pilot page numbers")
    p.add_argument("--pages", required=True, help="e.g. 1,4")
    p.set_defaults(func=cmd_pilot)

    p = sub.add_parser("scope", parents=[common], help="Change requested_output")
    p.add_argument("requested_output", choices=OUTPUTS)
    p.set_defaults(func=cmd_scope)

    p = sub.add_parser("assume", parents=[common], help="Record an agent assumption (never a confirmation)")
    p.add_argument("text")
    p.set_defaults(func=cmd_assume)

    p = sub.add_parser("capabilities", parents=[common], help="Record a capability snapshot as key=value pairs")
    p.add_argument("pairs", nargs="+")
    p.set_defaults(func=cmd_capabilities)

    p = sub.add_parser("template-offer", parents=[common], help="Track the one-time template save offer")
    p.add_argument("status", choices=TEMPLATE_STATUSES)
    p.add_argument("--quote")
    p.add_argument("--candidate-file")
    p.add_argument("--template-id")
    p.add_argument("--version")
    p.add_argument("--user-requested", action="store_true", help="The user asked again themselves")
    p.set_defaults(func=cmd_template_offer)

    p = sub.add_parser("status", parents=[common], help="Show decisions, gates, next steps and drift warnings")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (StateError, ValueError, RuntimeError, OSError) as exc:
        print(f"deck_state: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
