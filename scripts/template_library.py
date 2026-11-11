#!/usr/bin/env python3
"""Versioned, local, opt-in PPT design bundles. No network or generation calls.

Source paths are relative to candidate.json's parent. Archived style references
are relative to the version directory; applied references are relative to the
new deck root, matching extract_briefs.py's working-directory contract.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unicodedata

from PIL import Image

ROLES = {"style", "layout", "typography", "preview", "brand"}
VERSION_RE = re.compile(r"v[0-9]{3,}\Z")
SENSITIVE_KEYS = {"apikey", "accesstoken", "refreshtoken", "authorization", "password", "secret", "clientsecret", "cookie", "cookies"}
KNOWN_REFERENCES = {"styleRefImage", "styleRefImages", "layoutSourceImage", "layoutRefImages", "brandRefImage", "brandRefImages"}
BRAND_IDENTITY_BLOCKS = {"brand", "brandidentity", "brandassets", "brandguidelines", "brandconfig"}


def default_library() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")).expanduser() / "moyu-ppt" / "templates"


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return data


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def required_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def validate_name(value) -> str:
    name = unicodedata.normalize("NFC", required_text(value, "name"))
    if name in {".", ".."} or len(name) > 100 or any(c in name for c in "/\\") or any(ord(c) < 32 for c in name):
        raise ValueError("name cannot contain path separators/control characters or exceed 100 characters")
    return name


def template_id(name: str) -> str:
    name = validate_name(name)
    slug = "".join(c if c.isalnum() else "-" for c in name.casefold())
    slug = re.sub(r"-+", "-", slug).strip("-")[:60] or "template"
    return f"{slug}-{digest(name.encode('utf-8'))[:8]}"


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 100 or not all(c.isalnum() or c in "-_" for c in value):
        raise ValueError("Invalid template id; use an id returned by list/save")
    return value


def validate_version(value: str) -> str:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value) or int(value[1:]) <= 0:
        raise ValueError("version must look like v001")
    return value


def validate_approval(approval) -> dict:
    if not isinstance(approval, dict) or approval.get("status") != "approved" or approval.get("by") != "user":
        raise ValueError("Saving requires approval.status='approved' and approval.by='user'; use --dry-run before asking the user")
    required_text(approval.get("note"), "approval.note (the user's actual choice)")
    timestamp = required_text(approval.get("approved_at"), "approval.approved_at")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
    except ValueError as exc:
        raise ValueError("approval.approved_at must be an ISO timestamp with timezone") from exc
    return dict(approval)


def reject_secrets(value, trail=()) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z]", "", key.casefold())
            if normalized in SENSITIVE_KEYS:
                raise ValueError(f"Remove credential field from design configuration: {'.'.join(trail + (key,))}")
            reject_secrets(child, trail + (key,))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            reject_secrets(child, trail + (str(i),))


def reference_slots(config: dict) -> list[tuple]:
    """Find supported image references, including metadata wrappers and brand paths."""
    slots = set()

    def paths(value, trail):
        if value is None or value == "":
            return
        if isinstance(value, str):
            slots.add(trail)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                paths(item, trail + (i,))
        elif isinstance(value, dict):
            if "path" in value:
                paths(value["path"], trail + ("path",))
            else:
                for key, item in value.items():
                    paths(item, trail + (key,))
        else:
            raise ValueError(f"Image reference must be a path, mapping, list, or null: {trail}")

    def walk(value, trail=()):
        if isinstance(value, dict):
            for key, child in value.items():
                location = trail + (key,)
                is_reference = (key in KNOWN_REFERENCES or key in {"logo", "wordmark"}
                                or key.casefold().endswith(("image", "images", "path", "paths"))
                                or (location == ("typography", "references"))
                                or ("brand" in trail and key == "assets"))
                if is_reference:
                    paths(child, location)
                else:
                    walk(child, location)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                walk(child, trail + (i,))
    walk(config)
    return sorted(slots, key=lambda t: str(t))


def get_slot(config, trail):
    for part in trail:
        config = config[part]
    return config


def set_slot(config, trail, value) -> None:
    for part in trail[:-1]:
        config = config[part]
    config[trail[-1]] = value


def source_path(value, base: Path, field: str) -> Path:
    raw = required_text(value, field)
    if "://" in raw or raw.startswith("data:"):
        raise ValueError(f"{field} must be an explicit local image path, not a URL")
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def image_info(path: Path) -> tuple[bytes, str, list[int]]:
    try:
        data = path.read_bytes()
        with Image.open(io.BytesIO(data)) as im:
            im.load()  # Decode pixels, including files that pass verify but are truncated.
            if im.width <= 0 or im.height <= 0:
                raise ValueError("empty dimensions")
            extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif", "TIFF": ".tiff", "BMP": ".bmp"}.get(im.format)
            if extension is None:
                raise ValueError(f"Unsupported image format: {im.format}")
            size = [im.width, im.height]
        return data, extension, size
    except (OSError, ValueError, SyntaxError) as exc:
        raise ValueError(f"Cannot fully decode image {path}: {exc}") from exc


def prepare(candidate_path: Path, require_approval=True) -> dict:
    candidate_path = candidate_path.expanduser().resolve()
    candidate = read_json(candidate_path)
    if type(candidate.get("schema_version")) is not int or candidate["schema_version"] != 1:
        raise ValueError("candidate.schema_version must be 1")
    name = validate_name(candidate.get("name"))
    description = required_text(candidate.get("description"), "description")
    notes = required_text(candidate.get("design_notes"), "design_notes")
    tags = candidate.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
        raise ValueError("tags must be a list of nonempty strings")
    approval = validate_approval(candidate.get("approval")) if require_approval else None
    base = candidate_path.parent
    config_path = source_path(candidate.get("style_config"), base, "style_config")
    config = read_json(config_path)
    reject_secrets(config)
    stripped = []
    typography = config.get("typography")
    if isinstance(typography, dict) and isinstance(typography.get("roles"), dict):
        for role, values in typography["roles"].items():
            if isinstance(values, dict) and "sampleText" in values:
                del values["sampleText"]
                stripped.append(f"typography.roles.{role}.sampleText")
    raw_assets = candidate.get("assets", [])
    if not isinstance(raw_assets, list):
        raise ValueError("assets must be a list")
    assets, blobs, source_map, ids = [], {}, {}, set()
    for item in raw_assets:
        if not isinstance(item, dict):
            raise ValueError("Each asset must be an object")
        asset_id = required_text(item.get("id"), "asset.id")
        if asset_id in ids:
            raise ValueError(f"Duplicate asset id: {asset_id}")
        ids.add(asset_id)
        role = item.get("role")
        if role not in ROLES:
            raise ValueError(f"Invalid asset role: {role}")
        source = source_path(item.get("path"), base, f"asset {asset_id}.path")
        data, extension, size = image_info(source)
        sha = digest(data)
        relative = f"assets/{sha}{extension}"
        entry = {"id": asset_id, "role": role, "path": relative, "sha256": sha, "bytes": len(data), "size": size}
        for optional in ("source_url", "note"):
            if optional in item:
                entry[optional] = required_text(item[optional], f"asset.{optional}")
        if "source_url" in entry and not re.match(r"^https?://", entry["source_url"]):
            raise ValueError("asset.source_url must be an http(s) attribution URL")
        assets.append(entry)
        blobs[relative] = data
        source_map[source] = relative
    for slot in reference_slots(config):
        value = get_slot(config, slot)
        source = source_path(value, base, f"style_config {slot}")
        if source not in source_map:
            raise ValueError(f"Unlisted image reference at {slot}: {value}. Explicitly add it to candidate.assets or remove the reference.")
        set_slot(config, slot, source_map[source])
    warnings = []
    if stripped:
        warnings.append("Removed sampleText from typography roles; create fresh specimen copy for the next deck.")
    if any(a["role"] == "brand" for a in assets):
        warnings.append("Brand assets are saved by this choice only. Apply will keep them optional and remove automatic brand bindings; enable them only within the next task's brand scope.")
    if any(a["role"] == "preview" for a in assets):
        warnings.append("Preview images may contain old deck copy; use for visual comparison only, never as the next deck's content.")
    return {"id": template_id(name), "name": name, "description": description, "tags": list(dict.fromkeys(tag.strip() for tag in tags)), "design_notes": notes,
            "style": config, "assets": assets, "blobs": blobs, "approval": approval, "warnings": warnings, "removed_fields": stripped}


@contextmanager
def library_lock(library: Path):
    try:
        import fcntl
    except ImportError as exc:
        raise ValueError("Template saves currently require macOS/Linux file locking; this platform is not supported") from exc
    library.mkdir(parents=True, exist_ok=True)
    with (library / ".library.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Template library is busy with another save; retry when it completes") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def save(candidate_path: Path, library: Path, dry_run=False) -> dict:
    prepared = prepare(candidate_path, require_approval=not dry_run)
    summary = {key: prepared[key] for key in ("id", "name", "description", "tags", "assets", "warnings", "removed_fields")}
    if dry_run:
        return {"status": "preview", "saved": False, "approval_required": True, **summary}
    library = library.expanduser().resolve()
    with library_lock(library):
        parent = library / prepared["id"]
        if parent.is_symlink():
            raise ValueError("Template directory must not be a symlink")
        parent.mkdir(exist_ok=True)
        versions = [int(p.name[1:]) for p in parent.iterdir() if p.is_dir() and VERSION_RE.fullmatch(p.name)]
        version = f"v{max(versions, default=0) + 1:03d}"
        destination = parent / version
        temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=parent))
        try:
            (temporary / "assets").mkdir()
            for path, data in prepared["blobs"].items():
                (temporary / path).write_bytes(data)
            write_json(temporary / "style.json", prepared["style"])
            (temporary / "design-notes.md").write_text(prepared["design_notes"] + "\n", encoding="utf-8")
            metadata = {"schema_version": 1, **summary, "version": version, "created_at": datetime.now(timezone.utc).isoformat(), "approval": prepared["approval"],
                        "style_config": "style.json", "design_notes": "design-notes.md", "reference_path_base": "template_version_directory",
                        "files": {"style.json": digest((temporary / "style.json").read_bytes()), "design-notes.md": digest((temporary / "design-notes.md").read_bytes())}}
            write_json(temporary / "template.json", metadata)
            temporary.rename(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return {"status": "saved", "saved": True, "path": str(destination), "version": version, **summary}


def bundle_path(library: Path, identifier: str, version=None) -> Path:
    library = library.expanduser().resolve()
    parent = library / validate_id(identifier)
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError(f"Template does not exist or is not a regular directory: {identifier}")
    if version is None:
        versions = [p.name for p in parent.iterdir() if p.is_dir() and not p.is_symlink() and VERSION_RE.fullmatch(p.name)]
        if not versions:
            raise ValueError(f"No versions found for template: {identifier}")
        version = max(versions, key=lambda v: int(v[1:]))
    target = parent / validate_version(version)
    if not target.is_dir() or target.is_symlink():
        raise ValueError(f"Version does not exist or is not a regular directory: {version}")
    return target


def bundle_file(bundle: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts or "\\" in relative:
        raise ValueError(f"Unsafe bundle path: {relative}")
    path = bundle / relative
    if not path.resolve().is_relative_to(bundle.resolve()) or path.is_symlink() or any(p.is_symlink() for p in path.parents if p != bundle and p.is_relative_to(bundle)):
        raise ValueError(f"Bundle path escapes its directory: {relative}")
    return path


def inspect_bundle(bundle: Path) -> tuple[dict, dict]:
    metadata = read_json(bundle_file(bundle, "template.json"))
    if metadata.get("schema_version") != 1 or metadata.get("id") != bundle.parent.name or metadata.get("version") != bundle.name:
        raise ValueError("Template metadata does not match its directory")
    validate_approval(metadata.get("approval"))
    expected_files = metadata.get("files")
    if not isinstance(expected_files, dict) or set(expected_files) != {"style.json", "design-notes.md"}:
        raise ValueError("Template must hash style.json and design-notes.md")
    for relative, expected in expected_files.items():
        path = bundle_file(bundle, relative)
        try:
            actual = digest(path.read_bytes())
        except OSError as exc:
            raise ValueError(f"Missing template file: {relative}") from exc
        if actual != expected:
            raise ValueError(f"Hash mismatch: {relative}")
    assets = metadata.get("assets")
    if not isinstance(assets, list):
        raise ValueError("Invalid template asset list")
    paths = set()
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("role") not in ROLES:
            raise ValueError("Invalid template asset record")
        relative = asset.get("path")
        path = bundle_file(bundle, relative)
        if len(Path(relative).parts) != 2 or Path(relative).parts[0] != "assets":
            raise ValueError(f"Asset must be inside assets/: {relative}")
        data, _, size = image_info(path)
        if digest(data) != asset.get("sha256") or len(data) != asset.get("bytes") or size != asset.get("size"):
            raise ValueError(f"Hash or image metadata mismatch: {relative}")
        paths.add(relative)
    style = read_json(bundle_file(bundle, "style.json"))
    reject_secrets(style)
    for slot in reference_slots(style):
        if get_slot(style, slot) not in paths:
            raise ValueError(f"Unbound reference in archived style: {slot}")
    return metadata, style


def list_templates(library: Path, query="") -> dict:
    library = library.expanduser().resolve()
    matches, errors = [], []
    if library.is_dir():
        for path in sorted(library.glob("*/v*/template.json")):
            if path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink():
                errors.append({"path": str(path), "error": "Symlinked template ignored"})
                continue
            try:
                record = read_json(path)
                validate_id(path.parent.parent.name)
                validate_version(path.parent.name)
                if record.get("id") != path.parent.parent.name or record.get("version") != path.parent.name:
                    raise ValueError("Metadata identity mismatch")
                tags = record.get("tags", [])
                if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                    raise ValueError("Invalid template tags")
                searchable = " ".join([str(record.get("name", "")), str(record.get("description", "")), " ".join(tags)]).casefold()
                if all(term in searchable for term in query.casefold().split()):
                    matches.append({key: record.get(key) for key in ("id", "version", "name", "description", "tags", "created_at")})
            except (ValueError, TypeError) as exc:
                errors.append({"path": str(path), "error": str(exc)})
    matches.sort(key=lambda item: (item["id"], int(item["version"][1:])))
    return {"templates": matches, "errors": errors}


def show(library: Path, identifier: str, version=None) -> dict:
    bundle = bundle_path(library, identifier, version)
    metadata, style = inspect_bundle(bundle)
    return {**metadata, "path": str(bundle), "integrity": "verified", "style": style,
            "design_notes_text": (bundle / "design-notes.md").read_text(encoding="utf-8")}


def without_brand_bindings(style: dict, assets: list) -> dict:
    brand_paths = {a["path"] for a in assets if a["role"] == "brand"}
    # A file explicitly serving another role stays usable for that role.
    brand_paths -= {a["path"] for a in assets if a["role"] != "brand"}
    omit = object()

    def clean(value):
        if isinstance(value, str):
            return omit if value in brand_paths else value
        if isinstance(value, dict):
            if value.get("role") == "brand" or (isinstance(value.get("path"), str) and value["path"] in brand_paths):
                return omit
            return {key: result for key, item in value.items()
                    if re.sub(r"[^a-z]", "", key.casefold()) not in BRAND_IDENTITY_BLOCKS
                    and (result := clean(item)) is not omit}
        if isinstance(value, list):
            return [result for item in value if (result := clean(item)) is not omit]
        return value
    return clean(style)


def apply(library: Path, identifier: str, output: Path, version=None, deck_root=None) -> dict:
    bundle = bundle_path(library, identifier, version)
    metadata, style = inspect_bundle(bundle)
    deck_root = Path(deck_root or Path.cwd()).expanduser().resolve()
    if not deck_root.is_dir():
        raise ValueError("--deck-root must be an existing directory")
    output = output.expanduser()
    if not output.is_absolute():
        output = deck_root / output
    if output.is_symlink():
        raise ValueError("--output must not be a symlink")
    output = output.resolve()
    if not output.is_relative_to(deck_root) or output == deck_root:
        raise ValueError("--output must be a new directory inside --deck-root")
    if output.exists():
        raise ValueError("--output already exists; choose a new directory to avoid overwriting project files")
    if output.is_relative_to(library.expanduser().resolve()):
        raise ValueError("Apply output cannot be inside the template library")
    optional_brand_config = {key: value for key, value in style.items()
                             if re.sub(r"[^a-z]", "", key.casefold()) in BRAND_IDENTITY_BLOCKS}
    style = without_brand_bindings(style, metadata["assets"])
    prefix = output.relative_to(deck_root)
    for slot in reference_slots(optional_brand_config):
        set_slot(optional_brand_config, slot, (prefix / get_slot(optional_brand_config, slot)).as_posix())
    for slot in reference_slots(style):
        set_slot(style, slot, (prefix / get_slot(style, slot)).as_posix())
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".template-apply-", dir=output.parent))
    try:
        (temporary / "assets").mkdir()
        for asset in metadata["assets"]:
            source = bundle_file(bundle, asset["path"])
            data = source.read_bytes()
            if digest(data) != asset["sha256"]:
                raise ValueError(f"Asset changed during apply: {asset['path']}")
            (temporary / asset["path"]).write_bytes(data)
        write_json(temporary / "style.json", style)
        (temporary / "design-notes.md").write_bytes((bundle / "design-notes.md").read_bytes())
        applied = dict(metadata)
        applied.update({"reference_path_base": "deck_root", "applied_at": datetime.now(timezone.utc).isoformat(),
                        "template_relative_to_deck_root": prefix.as_posix(),
                        "available_optional_brand_assets": [{**asset, "path": (prefix / asset["path"]).as_posix()} for asset in metadata["assets"] if asset["role"] == "brand"],
                        "available_optional_brand_config": optional_brand_config,
                        "brand_bindings_enabled": False,
                        "files": {"style.json": digest((temporary / "style.json").read_bytes()), "design-notes.md": digest((temporary / "design-notes.md").read_bytes())}})
        write_json(temporary / "template.json", applied)
        # mkdir reserves the final name exclusively; a concurrent apply cannot
        # overwrite it. Rollback removes only this process's empty reservation.
        output.mkdir()
        try:
            temporary.rename(output)
        except BaseException:
            output.rmdir()
            raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"status": "applied", "id": metadata["id"], "version": metadata["version"], "path": str(output),
            "style_config": str(output / "style.json"), "reference_path_base": "deck_root", "deck_root": str(deck_root),
            "warnings": metadata.get("warnings", []), "available_optional_brand_assets": applied["available_optional_brand_assets"],
            "available_optional_brand_config": applied["available_optional_brand_config"]}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=default_library())
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("save", "list", "show", "apply"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--library", type=Path, default=argparse.SUPPRESS)
        if command == "save":
            sub.add_argument("--candidate", type=Path, required=True)
            sub.add_argument("--dry-run", action="store_true")
        elif command == "list":
            sub.add_argument("--query", default="")
        else:
            sub.add_argument("--id", required=True)
            sub.add_argument("--version")
            if command == "apply":
                sub.add_argument("--output", type=Path, required=True)
                sub.add_argument("--deck-root", type=Path)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "save":
            result = save(args.candidate, args.library, args.dry_run)
        elif args.command == "list":
            result = list_templates(args.library, args.query)
        elif args.command == "show":
            result = show(args.library, args.id, args.version)
        else:
            result = apply(args.library, args.id, args.output, args.version, args.deck_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
