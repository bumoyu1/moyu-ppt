#!/usr/bin/env python3
"""Read-only gates for a confirmed deck brief, visual proposal and pilot.

Records establish a verifiable workflow shape, not proof of human intent. The
agent must copy actual user decisions; this module never invents or writes them.
Artifact paths resolve relative to deck-state.json, not to the installed skill.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from deck_runtime import validate_image_file
import content_map
import page_copy

ROLES = ("display", "title", "body", "label", "data")
STAGES = ("plans", "pilot", "production")
# A bare acknowledgement/default is not an instruction to make all later
# choices. Shared with deck_state.py so recording and checking agree.
BARE_DEFAULTS = frozenset({"默认", "default", "好的", "好", "ok", "okay", "沉默", "silence", "无回复", "继续"})


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def positive_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def ratio(value):
    """Compare mathematical ratios; no rounding or default can change the brief."""
    if not isinstance(value, str) or not re.fullmatch(r"[1-9]\d*(?:\.\d+)?:[1-9]\d*(?:\.\d+)?", value):
        raise ValueError("aspect_ratio must be positive W:H, for example 16:9")
    width, height = value.split(":")
    return Fraction(width) / Fraction(height)


def timestamp(value):
    if not nonempty(value) or "T" not in value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None and parsed.utcoffset() is not None
    except ValueError:
        return False


def meaningful_rule(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(meaningful_rule(item) for item in value.values())
    if isinstance(value, list):
        return any(meaningful_rule(item) for item in value)
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def user_record(record, *, decision=False):
    return (isinstance(record, dict) and record.get("source") == "user"
            and nonempty(record.get("evidence")) and timestamp(record.get("confirmed_at"))
            and (not decision or record.get("status") == "confirmed"))


def check_workflow(state_path="deck-state.json", stage="plans", *, pages=None,
                   aspect_ratios=None, require_complete=False):
    """Return actionable missing evidence. This function never mutates state.

    `pages` and `aspect_ratios` are the actual planned/generated batch values;
    subsets are allowed unless require_complete is requested by full extraction.
    """
    state_path = Path(state_path)
    result = {"ok": False, "stage": stage, "state": str(state_path), "missing": [],
              "next_steps": [], "authorized_by": {}, "artifact_paths": {}, "image_evidence": {}}

    def missing(code, instruction):
        if code not in result["missing"]:
            result["missing"].append(code)
            result["next_steps"].append(instruction)

    if stage not in STAGES:
        missing("stage", "选择 plans、pilot 或 production 阶段。")
        return result
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        workflow = state.get("workflow") if isinstance(state, dict) else None
    except (OSError, ValueError) as exc:
        missing("workflow_state", f"读取有效的 {state_path}；先向用户展示页数、比例、受众、目的与图文融合占比并记录其决定。({type(exc).__name__})")
        return result
    if not isinstance(workflow, dict):
        missing("workflow", "旧状态没有 workflow 决策记录；补充实际用户确认，不能把旧 pace 或默认页数视为授权。")
        return result
    if type(workflow.get("schema_version")) is not int or workflow.get("schema_version") != 1:
        missing("workflow.schema_version", "使用 workflow.schema_version=1，保留原项目状态。")
    mode = workflow.get("mode")
    if mode not in ("standard", "autonomous"):
        missing("workflow.mode", "将流程明确记录为 standard；只有实际委托记录才可采用 autonomous。")

    brief = workflow.get("brief")
    if not isinstance(brief, dict):
        brief = {}
    result["brief"] = {key: brief.get(key) for key in ("page_count", "aspect_ratio", "audience", "purpose",
                                                       "visual_ratio", "visual_floor", "title_layout")}
    # Decks with title_layout_rules had the user choose how content-page titles are laid out.
    result["title_layout_rules"] = bool(workflow.get("title_layout_rules"))
    if result["title_layout_rules"] and brief.get("title_layout") not in ("uniform", "free"):
        missing("brief.title_layout", "向用户说明两种标题排版并记录选择：brief --title-layout uniform（统一版式）或 free（自由构图）。")
    # Decks with visual_budget_rules enforce the image/text budget per page (visual_budget.py).
    result["visual_budget_rules"] = bool(workflow.get("visual_budget_rules"))
    result["continuity_rules"] = bool(workflow.get("continuity_rules"))
    count = brief.get("page_count")
    if not positive_integer(count):
        missing("brief.page_count", "向用户展示建议页数并记录确认后的正整数 page_count，不能静默采用 12 页。")
    try:
        expected_ratio = ratio(brief.get("aspect_ratio"))
    except ValueError:
        expected_ratio = None
        missing("brief.aspect_ratio", "记录用户已确认的画布比例，例如 16:9 或 21:9。")
    for field in ("audience", "purpose"):
        if not nonempty(brief.get(field)):
            missing(f"brief.{field}", f"在简报中补充并向用户展示 {field}。")

    decisions = workflow.get("decisions")
    decisions = decisions if isinstance(decisions, dict) else {}
    delegation = workflow.get("delegation")
    delegated_scopes = set()
    if mode == "autonomous":
        scopes = delegation.get("scopes") if isinstance(delegation, dict) else None
        valid_delegation = (user_record(delegation) and isinstance(scopes, list)
            and bool(scopes) and all(isinstance(scope, str) and scope in ("intake", "copy", "visual", "pilot") for scope in scopes)
            and len(scopes) == len(set(scopes))
            and delegation["evidence"].strip().lower().strip("。.!！ ") not in BARE_DEFAULTS)
        if valid_delegation:
            delegated_scopes = set(scopes)
        else:
            missing("delegation", "记录真实的代决策授权文字、覆盖步骤和带时区时间；默认、沉默及旧 pace 字段不能代替。")
    # Decks that keep a copy sheet (page_copy.py) need it confirmed before any page is designed.
    result["page_copy_required"] = bool(workflow.get("page_copy_required"))
    required_decisions = (("intake",) + (("copy",) if result["page_copy_required"] else ()) + ("visual",)
                          + (("pilot",) if stage == "production" else ()))
    decision_steps = {
        "intake": "展示页数、比例、受众、目的与图文融合占比，并记录用户对此简报的实际确认。",
        "copy": "把每页要上屏的文字写成一份文案稿给用户看（新增和引语单独指出），并记录用户的实际确认。",
        "visual": "展示用户挑中的参考图整理成的方向、设计说明和字体方案，并记录用户对视觉方案的实际确认。",
        "pilot": "展示已生成的代表页，记录用户接受该效果后再进入整套生产。",
    }
    for name in required_decisions:
        if user_record(decisions.get(name), decision=True):
            result["authorized_by"][name] = "user_decision"
        elif name in delegated_scopes:
            result["authorized_by"][name] = "explicit_delegation"
        else:
            missing(f"decisions.{name}", decision_steps[name])

    artifacts = workflow.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    loaded = {}
    artifact_names = ["visual_proposal", "inspiration_review", "style_config"]
    if stage == "production":
        artifact_names.append("pilot_review")
    # The content skeleton comes before any page: required for decks that carry the flag,
    # checked whenever one is registered.
    skeleton_path = artifacts.get("content_map")
    if nonempty(skeleton_path):
        report = content_map.check_map(state_path.parent / skeleton_path, state_path)
        if report["ok"]:
            result["artifact_paths"]["content_map"] = str((state_path.parent / skeleton_path).resolve())
            result["content_map"] = {key: report[key] for key in ("type", "sections", "nodes", "off_screen", "pending")}
        else:
            missing("artifacts.content_map", "修正内容骨架：" + "；".join(report["problems"][:5]))
    elif workflow.get("content_map_required"):
        missing("artifacts.content_map", "先读完素材写出内容骨架（类型、主线、每节讲者要讲什么与原文依据），"
                                         "content_map.py check 通过并给用户看过后登记到 workflow.artifacts.content_map。")
    copy_path = artifacts.get("page_copy")
    if nonempty(copy_path):
        resolved = state_path.parent / copy_path
        report = page_copy.check_copy(resolved, state_path.parent / skeleton_path if nonempty(skeleton_path) else None,
                                      state_path, count)
        confirmed = (decisions.get("copy") or {}).get("artifacts_sha256") if isinstance(decisions.get("copy"), dict) else None
        if not report["ok"]:
            missing("artifacts.page_copy", "修正文案稿：" + "；".join(report["problems"][:5]))
        elif isinstance(confirmed, dict) and confirmed.get("page_copy") != hashlib.sha256(resolved.read_bytes()).hexdigest():
            missing("decisions.copy.changed", "文案稿在用户确认后改过；把改动给用户看，重新记录确认后再拆分。")
        else:
            result["artifact_paths"]["page_copy"] = str(resolved.resolve())
            result["page_copy"] = {key: report[key] for key in ("pages", "lines", "marks")}
    elif result["page_copy_required"]:
        missing("artifacts.page_copy", "骨架和页数确认后，把每页要上屏的文字写成文案稿 page-copy.md（每行标原文、压缩、新增或引语），"
                                       "page_copy.py check 通过并给用户看过后登记到 workflow.artifacts.page_copy。")
    for name in artifact_names:
        value = artifacts.get(name)
        try:
            if not nonempty(value):
                raise ValueError("missing path")
            path = (state_path.parent / value).resolve()
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                raise ValueError("empty file")
            loaded[name] = text
            result["artifact_paths"][name] = str(path)
        except (OSError, ValueError, UnicodeError):
            missing(f"artifacts.{name}", f"将实际完成且非空的 {name} 文件路径记录到 workflow.artifacts。")
    if "style_config" in loaded:
        try:
            style = json.loads(loaded["style_config"])
            typography = style.get("typography") if isinstance(style, dict) else None
            roles = typography.get("roles") if isinstance(typography, dict) else None
            roles = roles if isinstance(roles, dict) else {}
            for name in ROLES:
                if not isinstance(roles.get(name), dict) or not meaningful_rule(roles[name]):
                    missing(f"typography.roles.{name}", f"在 style_config 中写出 {name} 的具体字体规则，并纳入对用户展示的方案。")
        except (ValueError, TypeError):
            missing("style_config.json", "将 style_config 修正为有效 JSON 对象，并包含 typography.roles 五角色。")
    typography_decision = workflow.get("typography_decision")
    typography_decision = typography_decision if isinstance(typography_decision, dict) else {}
    specimen = typography_decision.get("specimen")
    if specimen not in ("generate", "provided", "skip"):
        missing("typography_decision.specimen", "明确字体样张选择 generate、provided 或 skip；不能省略字体方案。")
    for field in ("reason", "summary"):
        if not nonempty(typography_decision.get(field)):
            missing(f"typography_decision.{field}", f"记录字体样张选择的 {field}；summary 应是已向用户展示的字体方案摘要。")
    if specimen in ("generate", "provided") and stage in ("pilot", "production"):
        value = artifacts.get("typography_specimen")
        try:
            if not nonempty(value):
                raise ValueError("missing specimen path")
            result["image_evidence"]["typography_specimen"] = validate_image_file(state_path.parent / value)
        except (OSError, ValueError, ImportError, RuntimeError):
            missing("artifacts.typography_specimen", "先生成或校验实际字体样张，记录可完整解码的图片路径，再开始代表页。")

    pilot_pages = workflow.get("pilot_pages")
    valid_pilot = (isinstance(pilot_pages, list) and 1 <= len(pilot_pages) <= 3
        and all(positive_integer(page) for page in pilot_pages)
        and len(set(pilot_pages)) == len(pilot_pages)
        and positive_integer(count) and all(page <= count for page in pilot_pages))
    if not valid_pilot:
        missing("pilot_pages", "选择总页数范围内 1–3 个不同的代表页页码，写入 workflow.pilot_pages。")
    result["pilot_pages"] = pilot_pages
    if stage == "production":
        images = artifacts.get("pilot_images")
        if not isinstance(images, dict) or not images:
            missing("artifacts.pilot_images", "记录已展示的代表页影像，如 {\"1\":\"images/p01.png\",\"3\":\"images/p03.png\"}；必须覆盖 pilot_pages。")
        elif valid_pilot:
            for page in pilot_pages:
                value = images.get(str(page))
                try:
                    if not nonempty(value):
                        raise ValueError("missing pilot image")
                    result["image_evidence"][f"pilot.{page}"] = validate_image_file(state_path.parent / value)
                except (OSError, ValueError, ImportError, RuntimeError):
                    missing(f"artifacts.pilot_images.{page}", f"为代表页 P{page} 提供已完成且可完整解码的实际影像，不能仅记录口头完成状态。")
    if pages is not None:
        valid_pages = (isinstance(pages, list) and bool(pages)
            and all(positive_integer(page) for page in pages)
            and len(set(pages)) == len(pages))
        if not valid_pages:
            missing("batch.pages", "每个 deck job 必须有唯一正整数 page；不要混入无页码任务。")
        else:
            if positive_integer(count) and any(page > count for page in pages):
                missing("batch.page_count", f"批次页码超过已确认的 {count} 页；按确认页数调整方案。")
            if require_complete and positive_integer(count) and sorted(pages) != list(range(1, count + 1)):
                missing("plans.page_count", f"整套方案应完整包含已确认的 1–{count} 页；若确实只处理子集，使用 --allow-partial。")
            if stage == "pilot" and valid_pilot and not set(pages).issubset(set(pilot_pages)):
                missing("batch.pilot_pages", f"pilot 仅能生成已选代表页 {pilot_pages}；其余页面等待代表页通过。")
    if aspect_ratios is not None:
        for actual in aspect_ratios:
            try:
                if expected_ratio is not None and ratio(actual) != expected_ratio:
                    missing("batch.aspect_ratio", f"批次比例 {actual} 与确认的 {brief.get('aspect_ratio')} 不符；调整参数/尺寸或取得新的比例决定。")
                else:
                    ratio(actual)
            except ValueError:
                missing("batch.aspect_ratio", "批次每页必须使用有效且与简报一致的宽高比。")
    result["ok"] = not result["missing"]
    return result


def require_workflow(*args, **kwargs):
    result = check_workflow(*args, **kwargs)
    if not result["ok"]:
        detail = "\n".join(f"- {name}: {instruction}" for name, instruction in zip(result["missing"], result["next_steps"]))
        raise ValueError(f"Workflow gate blocked ({result['stage']}):\n{detail}")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="Read-only workflow validation; does not record or invent a decision")
    check.add_argument("--state", default="deck-state.json")
    check.add_argument("--stage", choices=STAGES, required=True)
    args = parser.parse_args(argv)
    result = check_workflow(args.state, args.stage)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
