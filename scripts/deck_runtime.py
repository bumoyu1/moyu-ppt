#!/usr/bin/env python3
"""Shared local validation and reference binding; no network or credentials."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path

SCRIPT_INTERFACE = "internal-module"

ROLE_ORDER = {"target": 0, "family": 1, "style": 1, "layout": 2,
              "typography": 3, "size": 3, "brand": 4, "reference": 5}
TYPOGRAPHY_ROLES = {"title", "body", "label", "data", "display"}
POSITIONAL_REFERENCE = re.compile(r"参考图\s*[0-9０-９]+|reference\s+image\s*#?\s*\d+", re.I)


def atomic_write(path, data):
    """Replace only after all bytes are on disk; leave the old file on failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reject_positional_references(prompt):
    match = POSITIONAL_REFERENCE.search(prompt)
    if match:
        raise ValueError(
            f"Ambiguous positional reference '{match.group()}'. Replace handwritten numbers "
            "with stable IDs such as [S1], [L-cover], [T-title], [C1] or [target]; "
            "the generator assigns image numbers after resolving references."
        )


def normalize_references(references, target_image=None, require_files=True):
    """Bind stable roles, deduplicate exact entries, and never guess legacy roles."""
    if not isinstance(references, list):
        raise ValueError("references must be a list of paths or role objects")
    result = []
    by_path = {}
    ids = {}
    if target_image:
        references = [{"path": str(target_image), "role": "target", "id": "target"}] + references
    for index, item in enumerate(references):
        legacy = isinstance(item, str)
        if legacy:
            item = {"path": item, "role": "reference", "id": f"R{index + 1}"}
        elif isinstance(item, dict):
            item = dict(item)
        else:
            raise ValueError("Each reference must be a path or an object")
        path = item.get("path")
        role = item.get("role", "reference")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Reference path must be nonempty")
        if role not in ROLE_ORDER:
            raise ValueError(f"Unknown reference role: {role}")
        if require_files and not Path(path).is_file():
            raise ValueError(f"Reference image not found: {path}")
        identity = str(Path(path).resolve())
        ref_id = item.get("id") or f"R{index + 1}"
        if not isinstance(ref_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", ref_id):
            raise ValueError(f"Invalid reference id: {ref_id!r}")
        if role == "typography":
            type_role = item.get("typography_role") or ref_id.removeprefix("T-")
            if type_role not in TYPOGRAPHY_ROLES:
                raise ValueError(f"Invalid typography role: {type_role}")
            item["typography_role"] = type_role
        item.update(path=path, role=role, id=ref_id)
        if identity in by_path:
            previous = by_path[identity]
            if previous["role"] == "target" and role in {"reference", "target"}:
                continue
            if previous["role"] != role:
                raise ValueError(f"Reference has conflicting roles: {path}: {previous['role']} / {role}")
            if previous["id"] != ref_id and not legacy:
                raise ValueError(f"Same reference path has different IDs: {path}")
            continue
        if ref_id in ids and ids[ref_id] != identity:
            raise ValueError(f"Reference ID [{ref_id}] is assigned to different files")
        ids[ref_id] = identity
        by_path[identity] = item
        result.append(item)
    return sorted(result, key=lambda item: ROLE_ORDER[item["role"]])


def reference_instructions(references):
    if not references:
        return ""
    rules = {
        "target": "这是待修页面。保留本页内容与构图，仅实施用户明确要求的修改。",
        "family": "仅传递本套PPT的画风、笔触、材质与色彩分级；不得沿用其布局或文字内容。",
        "style": "仅传递已指定的视觉风格、材质与色彩；跟随强度按正文配置，不沿用示例内容或布局。",
        "layout": "仅沿用标题位置、页边距、栅格与留白的版式骨架；不沿用配色、字体、文字与画面内容。",
        "typography": "仅向对应文字角色迁移字形骨架、笔画、字面重心与字距；不迁移样例文字、配色、布局或场景。",
        "size": "字号样板：与画布同比例，只标出带字号锁的几行字应有的字高和位置。本页同一行字画成同样高、放在同样位置；不照抄样板里的字，不沿用它的背景、配色或构图。",
        "brand": "这是品牌资产；按正文指定位置和资产类型保真要求使用，不作为整页风格来源。",
        "reference": "这是未指定角色的辅助参考；只按正文明确的用途使用，不推断其角色。",
    }
    if any(item["role"] == "size" for item in references):
        # A layout thumbnail can carry a very different title scale. Give the
        # same-canvas size guide sole authority over locked text geometry.
        rules["layout"] = (
            "仅借正文区的分栏、关系结构、栅格与留白；带字号锁文字的大小和定位由字号样板及本页尺寸指令确定，"
            "不从这张版式图继承标题位置或字号。不沿用配色、字体、文字与画面内容。"
        )
        rules["typography"] += " 字高和定位由字号样板及本页尺寸指令确定，不从紧裁字形图继承大小。"
        rules["size"] = (
            "这是完整画幅的字号几何样板，提供带字号锁文字的墨迹高度和定位锚点。"
            "按本页准确文案绘制，保留指定字形比例，行宽自然形成，不放大填满空白或拉伸。"
            "几何样板可能使用不同的原生字体；字形仍取本页指定字体或编辑底图。"
            "不照抄样板文字，不迁移其字体、颜色、背景、留白布局或场景。"
        )
    lines = ["【参考图绑定：以下序号与实际上传顺序一致；[ID]为正文中的稳定标识】"]
    for number, item in enumerate(references, 1):
        extra = f" 对应文字角色：{item['typography_role']}。" if item["role"] == "typography" else ""
        lines.append(f"参考图{number} [{item['id']}] ({item['role']})：{rules[item['role']]}{extra}")
    return "\n".join(lines)


def validate_image_bytes(data, expected_size=None, expected_ratio=None):
    """Fully decode with Pillow; a filename or PNG signature is not validation."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for image validation. Install with: python3 -m pip install Pillow") from exc
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            image_format = image.format
    except Exception as exc:
        raise ValueError(f"Response is not a decodable image: {type(exc).__name__}") from exc
    if expected_size:
        match = re.fullmatch(r"(\d+)[xX](\d+)", str(expected_size))
        if match and (width, height) != tuple(map(int, match.groups())):
            raise ValueError(f"Image dimensions {width}x{height} do not match requested {expected_size}")
    if expected_ratio:
        try:
            a, b = map(float, expected_ratio.split(":"))
            ratio = a / b
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError(f"Invalid aspect ratio: {expected_ratio}") from exc
        # Pixel rounding may move the ratio by at most about one pixel per side.
        if abs(width / height - ratio) > max(0.002 * ratio, 2 / height):
            raise ValueError(f"Image ratio {width}:{height} does not match {expected_ratio}")
    return {"width": width, "height": height, "format": image_format,
            "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def validate_image_file(path, expected_size=None, expected_ratio=None):
    return validate_image_bytes(Path(path).read_bytes(), expected_size, expected_ratio)
