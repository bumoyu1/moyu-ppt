#!/usr/bin/env python3
"""Prepare one role's neutral typography specimen; never calls an image provider.

Example:
  python3 typography_specimen.py --config style.json --role title \
    --text '让复杂问题变清楚' --output-dir assets/typography

The .jobs.json array is compatible with generate_slide_image.py --batch. Paths
are absolute so a later generator run can use a different working directory.
"""

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path


ROLES = ("display", "title", "body", "label", "data")
DEFAULTS = {
    "display": {
        "familyIntent": "与整套标题协调的中文展示字",
        "skeleton": "汉字结构完整，内部空间清楚",
        "stroke": "有力度但不粘连，不省略笔画",
        "center": "保持可辨的字形与稳定的视觉重心",
        "spacing": {"letterSpacingEm": 0.02, "lineHeight": 1.15},
    },
    "title": {
        "familyIntent": "清晰且稳定的现代中文标题字",
        "skeleton": "部首比例明确，内部空间开放",
        "stroke": "中粗且干净，完整保留笔画",
        "center": "重心与基线稳定",
        "spacing": {"letterSpacingEm": 0.01, "lineHeight": 1.2},
    },
    "body": {
        "familyIntent": "便于连续阅读的常规无衬线字",
        "skeleton": "标准部首结构，开放字腔",
        "stroke": "常规字重，不变形不断笔",
        "center": "基线稳定，字面均衡",
        "spacing": {"letterSpacingEm": 0, "lineHeight": 1.5},
    },
    "label": {
        "familyIntent": "清晰紧凑的常规辅助文字",
        "skeleton": "结构标准，小尺寸仍可辨认",
        "stroke": "笔画完整，不采用过细线条",
        "center": "对齐稳定，不逐字跳动",
        "spacing": {"letterSpacingEm": 0, "lineHeight": 1.3},
    },
    "data": {
        "familyIntent": "清晰可比较的数字与单位字形",
        "skeleton": "数字等高，0/O、1/l 易区分，便于列对齐",
        "stroke": "完整平涂，小数点负号和百分号可辨",
        "center": "基线稳定，单位与数值协调",
        "spacing": {"letterSpacingEm": 0, "lineHeight": 1.2},
    },
}
GLYPH_FIELDS = {
    "familyIntent": "字形方向",
    "skeleton": "骨架",
    "stroke": "笔画",
    "center": "重心",
}
LOCKED_POLICY = {
    "scope": "glyph-only",
    "preserveExactText": True,
    "transferColor": False,
    "transferLayout": False,
    "transferSampleText": False,
}


def _mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"{label} must be positive")
    return value


def role_config(config, role):
    """Read only selected role; legacy configs receive conservative defaults."""
    _mapping(config, "config")
    if role not in ROLES:
        raise ValueError(f"Unknown typography role: {role}; choose from {', '.join(ROLES)}")
    typography = _mapping(config.get("typography", {}), "typography")
    roles = _mapping(typography.get("roles", {}), "typography.roles")
    unknown = set(roles) - set(ROLES)
    if unknown:
        raise ValueError(f"Unknown typography role(s): {', '.join(sorted(unknown))}")
    policy = _mapping(typography.get("referencePolicy", {}), "typography.referencePolicy")
    if policy.get("mode", "as-needed") not in ("off", "as-needed"):
        raise ValueError("referencePolicy.mode must be off or as-needed")
    if policy.get("mode") == "off":
        raise ValueError("Typography referencePolicy.mode is off; no specimen job was prepared")
    for key, expected in LOCKED_POLICY.items():
        if key in policy and (type(policy[key]) is not type(expected) or policy[key] != expected):
            raise ValueError(f"referencePolicy.{key} must be {expected!r} for role-isolated specimens")
    supplied = _mapping(roles.get(role, {}), f"typography.roles.{role}")
    resolved = {**DEFAULTS[role], **supplied}
    for key in GLYPH_FIELDS:
        if not isinstance(resolved[key], str) or not resolved[key].strip():
            raise ValueError(f"typography.roles.{role}.{key} must be nonempty text")
    spacing = {**DEFAULTS[role]["spacing"], **_mapping(supplied.get("spacing", {}), "spacing")}
    _number(spacing["letterSpacingEm"], "letterSpacingEm")
    _number(spacing["lineHeight"], "lineHeight", positive=True)
    resolved["spacing"] = spacing
    effects = resolved.get("effects", [])
    if not isinstance(effects, list) or any(not isinstance(item, str) for item in effects):
        raise ValueError("effects must be an array of strings")
    return resolved


def build_specimen(config, role, text=None):
    """Return prompt and evidence metadata without writes or network access."""
    spec = role_config(config, role)
    if text is None:
        text = spec.get("sampleText")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Provide nonempty --text/--text-file or this role's sampleText")
    if "\x00" in text:
        raise ValueError("Visible text cannot contain NUL")
    # No stripping, Unicode normalization, translation or punctuation correction.
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    marker = f"EXACT_TEXT_{digest[:16]}"
    while marker in text:
        marker += "_"
    glyph_rules = "\n".join(f"- {label}：{spec[key]}" for key, label in GLYPH_FIELDS.items())
    spacing = spec["spacing"]
    prompt = (
        "制作一张单一字形方向的纯文字参考图，一图只有一套字形。\n"
        "以下边界之间是唯一允许出现在画面上的文字。边界本身与所有说明均不渲染。\n"
        "逐字保留输入，包括简繁、标点、大小写、数字、单位和换行；不得转换、改写或补字。\n"
        f"BEGIN_{marker}\n{text}\nEND_{marker}\n\n"
        "字形约束（仅为制作指令，不是可见文字）：\n"
        f"{glyph_rules}\n"
        f"- 字距起点：{spacing['letterSpacingEm']:g} em，不能粘连笔画。\n"
        f"- 多行行距起点：{spacing['lineHeight']:g} 倍，调整画布留白而不是删字。\n\n"
        "画面要求：纯白背景、黑色平涂文字，平面正视，无透视；只排列上述文字一次。"
        "四周保留安全空间，不裁切任何笔画；文字块自然居中，所有原文完整可见。\n"
        "无场景、人物、道具、照片、色板、版面分区、边框、背景纹理或外部 Logo。"
        "不添加任何角色名、英文标签、标题说明、图号、水印或与输入无关的文字。"
        "不添加发光、投影、立体材质、渐变、贴纸和装饰插图。\n"
        "这是字形视觉参考，不是整页设计。只建立骨架、笔画与重心；最终页面颜色、"
        "字号、布局、材质与装饰另行决定。发生冲突时按字符正确、清晰可读、字形特征的顺序取舍。"
    )
    return {
        "prompt": prompt,
        "metadata": {
            "schema_version": 1,
            "artifact_type": "typography-specimen-plan",
            "role": role,
            "reference_id": f"T-{role}",
            "expected_text": text,
            "text_sha256": digest,
            "scope": "glyph-only",
            "glyph_constraints": {key: spec[key] for key in GLYPH_FIELDS},
            "spacing": spacing,
            "omitted_effects": spec.get("effects", []),
            "transfer_policy": dict(LOCKED_POLICY),
            "image_generated": False,
            "visual_review": "missing evidence",
        },
    }


def prepare_specimen(config, role, text, output_dir, *, name=None, aspect_ratio="16:9", overwrite=False):
    result = build_specimen(config, role, text)
    name = name or role
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name):
        raise ValueError("name must contain only letters, numbers, underscores or hyphens")
    parts = aspect_ratio.split(":")
    if len(parts) != 2:
        raise ValueError("aspect_ratio must be positive W:H")
    try:
        [_number(float(part), "aspect_ratio", positive=True) for part in parts]
    except (ValueError, OverflowError) as exc:
        raise ValueError("aspect_ratio must be positive W:H") from exc
    folder = Path(output_dir).expanduser().resolve()
    prompt_path = folder / f"{name}.prompt.txt"
    jobs_path = folder / f"{name}.jobs.json"
    evidence_path = folder / f"{name}.specimen.json"
    image_path = folder / f"{name}.png"
    result["metadata"]["planned_image"] = str(image_path)
    jobs = [{
        "prompt_file": str(prompt_path),
        "output": str(image_path),
        "aspect_ratio": aspect_ratio,
    }]
    contents = {
        prompt_path: result["prompt"],
        jobs_path: json.dumps(jobs, ensure_ascii=False, indent=2) + "\n",
        evidence_path: json.dumps(result["metadata"], ensure_ascii=False, indent=2) + "\n",
    }
    if not overwrite:
        if image_path.exists() and not evidence_path.exists():
            raise ValueError(f"Untracked image exists: {image_path}; use a new --name")
        for path, content in contents.items():
            if path.exists() and path.read_bytes().decode("utf-8") != content:
                raise ValueError(f"Different specimen already exists: {path}; use a new --name or --overwrite")
    folder.mkdir(parents=True, exist_ok=True)
    for path, content in contents.items():
        path.write_bytes(content.encode("utf-8"))
    return {
        "role": role,
        "prompt_file": str(prompt_path),
        "jobs_file": str(jobs_path),
        "specimen_file": str(evidence_path),
        "planned_image": str(image_path),
        "status": "prepared; image generation and visual review are pending",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Style config JSON (legacy configs accepted)")
    parser.add_argument("--role", choices=ROLES, required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--text", help="Exact text, unchanged")
    source.add_argument("--text-file", help="UTF-8 file with exact text; no normalization")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--name", help="Artifact base name, defaults to role")
    parser.add_argument("--aspect-ratio", default="16:9")
    parser.add_argument("--overwrite", action="store_true", help="Replace differing plan files; never modifies an image")
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        # newline='' preserves CRLF instead of the universal-newline conversion.
        if args.text_file:
            with Path(args.text_file).open(encoding="utf-8", newline="") as stream:
                exact_text = stream.read()
        else:
            exact_text = args.text
        result = prepare_specimen(config, args.role, exact_text, args.output_dir,
                                  name=args.name, aspect_ratio=args.aspect_ratio,
                                  overwrite=args.overwrite)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
