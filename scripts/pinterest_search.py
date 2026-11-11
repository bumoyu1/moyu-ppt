#!/usr/bin/env python3
"""Search Pinterest via ScrapeCreators and return normalized reference candidates.

Search, exact deduplication and review-pool preparation are deterministic.
Visual relevance, template-family similarity and diversity remain review tasks.
Existing query arguments, download flags and originals mode remain supported.
A query plan records search intent; --require-brief checks the documented
pre-search decision and design coverage before side effects. It checks records,
not taste or the authenticity of a user's decision. Local selection makes a
separate display pool without querying the provider again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


ENDPOINT = "https://api.scrapecreators.com/v1/pinterest/search"
RETRY_STATUSES = {429, 500, 502, 503, 504}
KEYCHAIN_SERVICE = "scrapecreators_pinterest_api_key"
DOWNLOAD_TIMEOUT = 20.0
TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "epik", "mc_cid", "mc_eid"}
QUERY_METADATA = ("direction", "purpose", "target", "basis")
QUERY_PURPOSES = {"system", "layout", "typography", "image-language", "material"}
# Two kinds of reference, searched in separate groups and combined after the user picks:
# slides to lay pages out from, and pictures of the style itself (interface, icons, scenes,
# textures, emblems) to draw them in. When the user named a style, both kinds stay on it:
# generic business layouts have nothing to do with what the user asked to see.
LAYOUT_PURPOSES = {"system", "layout"}
PRESENTATION_WORDS = ("presentation", "slide", "deck", "keynote", "powerpoint", "ppt",
                      "演示", "幻灯", "简报", "课件", "スライド", "プレゼン")


class PinterestSearchError(RuntimeError):
    """Raised when the ScrapeCreators request cannot be completed."""


def load_api_key(explicit_key: str | None = None) -> str | None:
    if explicit_key:
        return explicit_key
    env_key = os.environ.get("SCRAPECREATORS_API_KEY")
    if env_key:
        return env_key
    try:  # key saved by configure.py set-key scrapecreators
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import local_config
        stored = local_config.read_credential("scrapecreators")
    except Exception:
        stored = None
    if stored:
        return stored
    if sys.platform != "darwin":
        return None

    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                os.environ.get("USER", ""),
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None

    key = result.stdout.strip()
    return key or None


def _request_json(url: str, api_key: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "x-api-key": api_key,
            "accept": "application/json",
            "user-agent": "moyu-ppt/1.0",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").replace(api_key, "[redacted]")[:500]
        raise PinterestSearchError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise PinterestSearchError(f"Network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise PinterestSearchError("Request timed out") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PinterestSearchError("Response was not valid JSON") from exc

    if not isinstance(payload, dict):
        raise PinterestSearchError("Response JSON was not an object")
    return payload


def search_pinterest(
    query: str,
    api_key: str,
    *,
    cursor: str | None = None,
    trim: bool = True,
    timeout: float = 20.0,
    retries: int = 1,
) -> dict[str, Any]:
    params = {"query": query, "trim": str(trim).lower()}
    if cursor:
        params["cursor"] = cursor
    url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"

    last_error: PinterestSearchError | None = None
    for attempt in range(retries + 1):
        try:
            return _request_json(url, api_key, timeout)
        except PinterestSearchError as exc:
            last_error = exc
            message = str(exc)
            should_retry = any(f"HTTP {status}" in message for status in RETRY_STATUSES)
            if attempt >= retries or not should_retry:
                break
            time.sleep(min(2**attempt, 4))

    raise last_error or PinterestSearchError("Unknown Pinterest search error")


def _image_from_pin(pin: dict[str, Any]) -> dict[str, Any]:
    images = pin.get("images") if isinstance(pin.get("images"), dict) else {}
    orig = images.get("orig") if isinstance(images.get("orig"), dict) else {}
    return {
        "url": orig.get("url"),
        "width": orig.get("width"),
        "height": orig.get("height"),
    }


def normalize_pins(payload: dict[str, Any], *, query: str, limit: int | None = None) -> list[dict[str, Any]]:
    if payload.get("success") is not True:
        raise PinterestSearchError(
            f"API returned unsuccessful response: {payload.get('error') or payload.get('message') or 'unknown error'}"
        )

    raw_pins = payload.get("pins")
    if not isinstance(raw_pins, list):
        raise PinterestSearchError("API response did not include a pins list")

    pins: list[dict[str, Any]] = []
    for pin in raw_pins:
        if not isinstance(pin, dict):
            continue
        image = _image_from_pin(pin)
        if not image.get("url"):
            continue
        board = pin.get("board") if isinstance(pin.get("board"), dict) else {}
        pins.append(
            {
                "id": pin.get("id"),
                "query": query,
                "pinterest_url": pin.get("url"),
                "image_url": image.get("url"),
                "image_width": image.get("width"),
                "image_height": image.get("height"),
                "title": pin.get("title") or pin.get("grid_title") or "",
                "description": pin.get("description") or "",
                "alt_text": pin.get("auto_alt_text") or pin.get("seo_alt_text") or "",
                "domain": pin.get("domain") or "",
                "source_link": pin.get("link") or "",
                "board_name": board.get("name") or "",
                "created_at": pin.get("created_at") or "",
                "is_promoted": bool(pin.get("is_promoted")),
            }
        )

    if limit is not None:
        pins = pins[: max(limit, 0)]
    return pins


def canonical_url(url: str | None, *, image: bool = False) -> str:
    """Normalize known duplicate URL forms without dropping functional query keys."""
    if not url:
        return ""
    parts = urllib.parse.urlsplit(str(url).strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not host:
        return str(url).strip()
    netloc = host
    if parts.port and parts.port not in {80, 443}:
        netloc += f":{parts.port}"
    path = parts.path or "/"
    params = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    params = [(k, v) for k, v in params if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    if image and (host == "pinimg.com" or host.endswith(".pinimg.com")):
        path = re.sub(r"^/(?:\d{2,4}x(?:\d{2,4})?|originals)/", "/originals/", path)
        params = [(k, v) for k, v in params if k.lower() not in {"w", "h", "width", "height", "quality", "format"}]
    return urllib.parse.urlunsplit(("https", netloc, path, urllib.parse.urlencode(sorted(params)), ""))


def _fold(text: str) -> str:
    """Case, width and spacing do not matter when looking for a style's name inside a keyword."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


def validate_search_brief(payload: Any, queries: list[str]) -> dict[str, Any]:
    """Validate structure and declared coverage, not aesthetics or user intent."""
    if queries:
        raise ValueError("--require-brief does not allow positional queries; include every query in the plan.")
    if not isinstance(payload, dict):
        raise ValueError("--require-brief needs a --query-plan JSON object with brief and queries.")
    brief = payload.get("brief")
    if not isinstance(brief, dict):
        raise ValueError("The query plan needs a brief object before search.")
    for key in ("style_intent", "audience_use", "typography"):
        if not isinstance(brief.get(key), str) or not brief[key].strip():
            raise ValueError(f"brief.{key} must be a nonempty string.")
    for key in ("content_needs", "avoid"):
        value = brief.get(key)
        if not isinstance(value, list) or (key == "content_needs" and not value):
            raise ValueError(f"brief.{key} must be {'a nonempty' if key == 'content_needs' else 'a'} list of strings.")
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError(f"brief.{key} items must be nonempty strings.")
    if brief.get("exploration") not in ("bounded", "open"):
        raise ValueError("brief.exploration must be bounded or open.")
    decision = brief.get("decision")
    if not isinstance(decision, dict) or decision.get("source") != "user" or decision.get("status") != "confirmed":
        raise ValueError("brief.decision must record source=user and status=confirmed before search.")
    if not isinstance(decision.get("evidence"), str) or not decision["evidence"].strip():
        raise ValueError("brief.decision.evidence must contain the user's style choice or permission to explore.")
    if brief.get("scope") not in ("directions", "supplement"):
        raise ValueError("brief.scope must be directions or supplement.")
    if brief["scope"] == "supplement" and (not isinstance(brief.get("existing_style"), str) or not brief["existing_style"].strip()):
        raise ValueError("brief.existing_style must explain the fixed system for a supplement search.")
    # A style the user named (a work, a game, a brand, an artist, a genre) anchors every keyword:
    # widening to look-alike renderings returns decks that have nothing to do with it.
    terms = brief.get("style_terms")
    if not isinstance(terms, list) or any(not isinstance(term, str) or not term.strip() for term in terms):
        raise ValueError("brief.style_terms must list the names the user's chosen style goes by (their own word plus "
                         "its names in the other languages searched); use [] only when the user named no style.")
    anchors = [_fold(term) for term in terms]

    entries = payload.get("queries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("The query plan needs a nonempty queries array.")
    directions: dict[str, set[str]] = {}
    for index, item in enumerate(entries, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Query {index} must be an object with query, direction, purpose, target and basis.")
        for key in ("query", *QUERY_METADATA):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"Query {index}.{key} must be a nonempty string.")
        purpose = item["purpose"].strip()
        if purpose not in QUERY_PURPOSES:
            raise ValueError(f"Query {index}.purpose must be one of: {', '.join(sorted(QUERY_PURPOSES))}.")
        folded = _fold(item["query"])
        if purpose in LAYOUT_PURPOSES and not any(_fold(word) in folded for word in PRESENTATION_WORDS):
            raise ValueError(
                f"Query {index} \"{item['query'].strip()}\" is a {purpose} keyword without a presentation word "
                "(slides, deck, keynote, PPT, 幻灯片, スライド ...). Layout references must be slide pages a deck can be "
                "laid out from; pictures of the style itself go in an image-language or material group.")
        beyond = item.get("beyond_style")
        if beyond is not None and (not isinstance(beyond, str) or not beyond.strip()):
            raise ValueError(f"Query {index}.beyond_style must quote the user's words asking to widen the search.")
        # A named style anchors every keyword: its decks, its layouts by page type, its elements.
        if anchors and not beyond and not any(anchor in folded for anchor in anchors):
            raise ValueError(
                f"Query {index} \"{item['query'].strip()}\" leaves the style the user named ({' / '.join(terms)}). "
                "With a named style every keyword keeps its name: its decks, its layouts by page type, its elements. "
                "Generic layouts without the style's name (flow charts, comparison slides, pricing tables) and "
                "look-alike styles are searched only when the user asks: list them for the user, and once they "
                "agree add beyond_style with their words to those keywords.")
        directions.setdefault(item["direction"].strip(), set()).add(purpose)
    mixed = sorted(name for name, purposes in directions.items() if purposes & LAYOUT_PURPOSES and purposes - LAYOUT_PURPOSES)
    if mixed:
        raise ValueError("Keep layout keywords and style-element keywords in separate groups, so the user can pick "
                         "from each: " + ", ".join(mixed))
    if brief["scope"] == "directions":
        used = set().union(*directions.values())
        if not used & LAYOUT_PURPOSES:
            raise ValueError("The plan has no layout keywords: add at least one group that searches slide layouts "
                             "(purpose layout or system).")
        if anchors and not used - LAYOUT_PURPOSES:
            raise ValueError("A named style needs style-element groups: search the style's own pictures (interface, icons, "
                             "scenes, textures, emblems) with purpose image-language or material, so that layouts and "
                             "the look can be combined.")
    return brief


def load_search_plan(queries: list[str], plan_path: str | None = None, *, require_brief: bool = False) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    """Load once, retaining the brief and query provenance through later selection."""
    payload: Any = None
    entries: list[Any] = []
    if plan_path:
        payload = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        entries = payload.get("queries") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise ValueError("Query plan must be an array or an object with a queries array.")
    brief = validate_search_brief(payload, queries) if require_brief else (payload.get("brief") if isinstance(payload, dict) else None)
    entries = entries + list(queries)
    plan: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        item = {"query": entry} if isinstance(entry, str) else entry
        if not isinstance(item, dict) or not isinstance(item.get("query"), str) or not item["query"].strip():
            raise ValueError("Each query-plan item needs a nonempty query string.")
        clean = {"query": item["query"].strip()}
        for key in QUERY_METADATA:
            if key in item:
                if not isinstance(item[key], str):
                    raise ValueError(f"Query-plan {key} must be a string.")
                clean[key] = item[key].strip()
        if isinstance(item.get("beyond_style"), str) and item["beyond_style"].strip():
            clean["beyond_style"] = item["beyond_style"].strip()
        fingerprint = json.dumps(clean, sort_keys=True, ensure_ascii=False)
        if fingerprint not in seen:
            plan.append(clean)
            seen.add(fingerprint)
    return plan, brief


def load_query_plan(queries: list[str], plan_path: str | None = None) -> list[dict[str, str]]:
    """Legacy list-only API; optional metadata and plain queries remain supported."""
    return load_search_plan(queries, plan_path)[0]


def merge_candidates(
    results: dict[str, list[dict[str, Any]]],
    query_plan: list[dict[str, str]],
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Merge identical pins/images, retain provenance, then interleave stably.

    A shared source page is recorded as a group, not proof of identical images:
    a gallery or template listing can legitimately contain several page designs.
    """
    rows: list[dict[str, Any]] = []
    for query in sorted(results):
        for rank, pin in enumerate(results[query]):
            rows.append({**pin, "query": query, "query_rank": rank})
    parents = list(range(len(rows)))

    def root(i: int) -> int:
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    seen: dict[str, int] = {}
    for i, pin in enumerate(rows):
        keys = {"image:" + canonical_url(pin.get("image_url"), image=True)}
        if pin.get("id") is not None and str(pin["id"]).strip():
            keys.add("pin:" + str(pin["id"]))
        if pin.get("pinterest_url"):
            keys.add("pin-url:" + canonical_url(pin["pinterest_url"]))
        for key in sorted(keys):
            if key in seen:
                parents[root(i)] = root(seen[key])
            else:
                seen[key] = i
    groups: dict[int, list[dict[str, Any]]] = {}
    for i, pin in enumerate(rows):
        groups.setdefault(root(i), []).append(pin)
    candidates: list[dict[str, Any]] = []
    for group in groups.values():
        ordered = sorted(group, key=lambda p: (canonical_url(p.get("image_url"), image=True), str(p.get("id") or ""), p["query"]))
        representative = ordered[0]
        image_keys = sorted({canonical_url(p["image_url"], image=True) for p in group})
        candidate_id = "ref_" + hashlib.sha256(image_keys[0].encode()).hexdigest()[:16]
        matched = sorted({p["query"] for p in group})
        source_links = sorted({canonical_url(p.get("source_link")) for p in group if p.get("source_link")})
        ranks = {q: min(p["query_rank"] for p in group if p["query"] == q) for q in matched}
        candidate = {k: v for k, v in representative.items() if k != "query_rank"}
        candidate.update({
            "candidate_id": candidate_id,
            "matched_queries": matched,
            "query_contexts": sorted([p for p in query_plan if p["query"] in matched], key=lambda p: json.dumps(p, sort_keys=True)),
            "pin_ids": sorted({str(p["id"]) for p in group if p.get("id") is not None}),
            "pinterest_urls": sorted({p["pinterest_url"] for p in group if p.get("pinterest_url")}),
            "canonical_image_url": image_keys[0],
            "canonical_source_links": source_links,
            "query_ranks": ranks,
            "local_path": None,
            "download_status": "not_requested",
            "review_index": None,
            "display_index": None,
        })
        candidates.append(candidate)
    # Alphabetical query order makes completion timing and argument order irrelevant.
    by_query = {q: sorted([p for p in candidates if q in p["matched_queries"]], key=lambda p: (p["query_ranks"][q], p["candidate_id"])) for q in sorted(results)}
    interleaved: list[dict[str, Any]] = []
    included: set[str] = set()
    while any(by_query.values()) and len(interleaved) < max(limit, 0):
        for query in by_query:
            while by_query[query] and by_query[query][0]["candidate_id"] in included:
                by_query[query].pop(0)
            if by_query[query] and len(interleaved) < max(limit, 0):
                pin = by_query[query].pop(0)
                included.add(pin["candidate_id"])
                interleaved.append(pin)
    for index, pin in enumerate(interleaved, 1):
        pin["candidate_index"] = index
    return interleaved, {"raw_count": len(rows), "unique_count": len(candidates), "exact_duplicates_removed": len(rows) - len(candidates), "retained_count": len(interleaved)}


def preview_variant(image_url: str) -> str:
    """Return the 736x preview variant of a pinimg originals URL (much smaller download)."""
    return re.sub(r"/originals/", "/736x/", image_url, count=1)


def original_variant(image_url: str) -> str:
    """Return the full-resolution originals variant of any pinimg size-variant URL."""
    return re.sub(r"/\d{2,4}x(?:\d{2,4})?/", "/originals/", image_url, count=1)


MIN_ORIGINAL_WIDTH = 1200


def download_original(image_url: str, out_path: str) -> dict[str, Any]:
    """Download the full-resolution original of a reference image.

    Use this for the reference the user selected, before cropping layout
    anchors — the 736x preview is too soft to crop from. Returns pixel
    dimensions when Pillow is available. For multi-slide collage sheets,
    per-cell crop width is roughly sheet_width / columns; a soft-looking
    [L] crop is usually structural (collage division), not a reason to
    discard the best-fitting template and re-search.
    """
    url = original_variant(image_url)
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"user-agent": "moyu-ppt/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            dest.write_bytes(response.read())
    except Exception:
        if url == image_url:
            raise
        # originals variant may 404 for some CDNs; fall back to the given URL
        url = image_url
        request = urllib.request.Request(url, headers={"user-agent": "moyu-ppt/1.0"})
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            dest.write_bytes(response.read())

    result: dict[str, Any] = {"local_path": str(dest), "source_url": url, "bytes": dest.stat().st_size}
    try:
        from PIL import Image

        with Image.open(dest) as img:
            result["width"], result["height"] = img.size
        if result["width"] < MIN_ORIGINAL_WIDTH:
            result["warning"] = (
                f"Original sheet is {result['width']}px wide (<{MIN_ORIGINAL_WIDTH}px). "
                "Inspect whether its layout structure remains legible. Keep a fitting "
                "reference when it is usable; if information is unreadable, seek the "
                "same source at better quality or report the limitation."
            )
    except ImportError:
        pass
    return result


def validate_preview(path: Path) -> str:
    """Decode when Pillow is installed; never claim visual/semantic validation."""
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("Preview file is missing or empty.")
    try:
        from PIL import Image
    except ImportError:
        return "unverified_no_pillow"
    with Image.open(path) as img:
        img.load()
    return "decoded"


def _download_one(pin: dict[str, Any], dest: Path, index: int) -> dict[str, Any]:
    candidate_id = pin.get("candidate_id") or "ref_" + hashlib.sha256(canonical_url(pin["image_url"], image=True).encode()).hexdigest()[:16]
    suffix = Path(urllib.parse.urlparse(pin["image_url"]).path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        suffix = ".jpg"
    out_path = dest / f"{candidate_id}{suffix}"
    urls = list(dict.fromkeys([preview_variant(pin["image_url"]), pin["image_url"]]))
    last_error: Exception | None = None
    for url in urls:
        try:
            request = urllib.request.Request(url, headers={"user-agent": "moyu-ppt/1.0"})
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
                out_path.write_bytes(response.read())
            validation = validate_preview(out_path)
            return {"id": pin.get("id"), "candidate_id": candidate_id, "local_path": str(out_path), "preview_validation": validation}
        except Exception as exc:
            last_error = exc
            out_path.unlink(missing_ok=True)
    raise last_error or PinterestSearchError("Preview download failed.")


def download_previews(pins: list[dict[str, Any]], download_dir: str, count: int, workers: int) -> list[dict[str, Any]]:
    dest = Path(download_dir)
    dest.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    next_index = 0
    succeeded = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Refill only from this retained pool; no hidden provider calls or unbounded retries.
        while next_index < len(pins) and succeeded < max(count, 0):
            stop = min(len(pins), next_index + max(count, 0) - succeeded)
            targets = pins[next_index:stop]
            futures = {pool.submit(_download_one, pin, dest, next_index + i + 1): pin for i, pin in enumerate(targets)}
            next_index = stop
            for future in as_completed(futures):
                pin = futures[future]
                try:
                    info = future.result()
                    if not info.get("local_path"):
                        raise ValueError("Download returned no preview path.")
                    pin.update({"local_path": info["local_path"], "download_status": "downloaded", "preview_validation": info.get("preview_validation", "unverified")})
                    pin.pop("download_error", None)
                    succeeded += 1
                except Exception as exc:
                    info = {"id": pin.get("id"), "candidate_id": pin.get("candidate_id"), "local_path": None, "error": str(exc)[:200]}
                    pin.update({"local_path": None, "download_status": "failed", "download_error": info["error"]})
                results.append(info)
    for index, pin in enumerate([p for p in pins if p.get("local_path")], 1):
        pin["review_index"] = index
    return results


def build_contact_sheet(
    pins: list[dict[str, Any]],
    out_path: str,
    *,
    cell_width: int = 420,
    cell_height: int = 420,
    columns: int = 4,
    index_field: str = "review_index",
) -> str | None:
    """Compose downloaded previews into one numbered grid image.

    This is an overview; reviewers may inspect individual finalists. Returns the sheet path, or
    None when Pillow is unavailable or nothing was downloaded.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    downloaded = [p for p in pins if p.get("local_path")]
    if not downloaded:
        return None

    count = len(downloaded)
    cols = min(columns, count)
    rows = (count + cols - 1) // cols
    label_band = 52
    pad = 10
    sheet = Image.new(
        "RGB",
        (cols * (cell_width + pad) + pad, rows * (cell_height + label_band + pad) + pad),
        "#FFFFFF",
    )
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 24)
    except Exception:
        font = ImageFont.load_default()

    for i, pin in enumerate(downloaded):
        col, row = i % cols, i // cols
        x0 = pad + col * (cell_width + pad)
        y0 = pad + row * (cell_height + label_band + pad)
        try:
            img = Image.open(pin["local_path"]).convert("RGB")
        except Exception:
            continue
        img.thumbnail((cell_width, cell_height))
        sheet.paste(img, (x0 + (cell_width - img.width) // 2, y0 + (cell_height - img.height) // 2))
        number = pin.get(index_field) or i + 1
        # Use the exact same index and stable id recorded in JSON.
        pin[index_field] = number
        draw.text((x0 + 4, y0 + cell_height + 4), f"[{number}] {pin.get('candidate_id', '')}", fill="#B03050", font=font)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, quality=88)
    return out_path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search Pinterest via ScrapeCreators (multiple queries in parallel) and normalize the response."
    )
    parser.add_argument("queries", nargs="*", help="One or more Pinterest search queries.")
    parser.add_argument("--query-plan", help="JSON query plan; entries can carry direction, purpose, target and basis.")
    parser.add_argument("--require-brief", action="store_true", help="For new skill-driven searches, require a confirmed style brief and design coverage for every direction.")
    parser.add_argument("--validate-plan", action="store_true", help="Validate --query-plan and print a JSON summary without credentials, network calls or output writes. Combine with --require-brief for strict preflight.")
    parser.add_argument("--input", help="Existing refs JSON for local-only selection; no API key is read.")
    parser.add_argument("--select-candidate", action="append", default=[], metavar="CANDIDATE_ID", help="Select a visually reviewed candidate from --input; repeat in display order.")
    parser.add_argument(
        "--download-original",
        metavar="IMAGE_URL",
        help="Standalone mode: download the full-resolution original of a reference image "
        "(use the selected pin's image_url from refs.json). Requires --output as the destination "
        "file path; no API key or queries needed. Prints JSON with local_path and pixel size.",
    )
    parser.add_argument("--cursor", help="Pagination cursor (only valid with a single query).")
    parser.add_argument("--api-key", help="ScrapeCreators API key. Prefer SCRAPECREATORS_API_KEY instead.")
    parser.add_argument("--timeout", type=float, default=20.0, help="Per-request timeout in seconds. Default: 20.")
    parser.add_argument("--retries", type=int, default=1, help="Retries for 429/5xx responses. Default: 1.")
    parser.add_argument("--limit", type=int, default=24, help="Maximum merged pins to return. Default: 24.")
    parser.add_argument("--per-query-limit", type=int, default=8, help="Maximum pins kept per query. Default: 8.")
    parser.add_argument("--download-dir", help="Download top candidate images (736x previews) into this directory.")
    parser.add_argument("--download-count", type=int, default=14, help="Legacy review-pool download target. Default: 14; not a final selection count.")
    parser.add_argument("--review-count", type=int, help="Successful review previews to prepare, refilling failures from the retained pool. Overrides --download-count.")
    parser.add_argument(
        "--contact-sheet",
        nargs="?",
        const="__auto__",
        help="Compose downloaded previews into one numbered grid image. "
        "Optional value = output path; default: {download-dir}/contact-sheet.jpg.",
    )
    parser.add_argument("--workers", type=int, default=4, help="Parallel workers for searches/downloads. Default: 4.")
    parser.add_argument("--output", help="Optional path to write normalized JSON.")
    trim_group = parser.add_mutually_exclusive_group()
    trim_group.add_argument("--trim", dest="trim", action="store_true", default=True, help="Request trimmed response. Default.")
    trim_group.add_argument("--no-trim", dest="trim", action="store_false", help="Request full response.")
    return parser.parse_args(argv)


def write_result(result: dict[str, Any], output_path: str | None) -> None:
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)


def pool_entry(pin: dict[str, Any], index_field: str) -> dict[str, Any]:
    return {"candidate_id": pin["candidate_id"], index_field: pin[index_field], "local_path": pin["local_path"], "preview_validation": pin.get("preview_validation", "unverified")}


def select_local_candidates(args: argparse.Namespace) -> dict[str, Any]:
    if args.queries or args.query_plan or args.download_original or args.cursor:
        raise ValueError("--input selection cannot be combined with search or original-download options.")
    if not args.select_candidate:
        raise ValueError("--input requires at least one --select-candidate ID after visual review.")
    result = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if not isinstance(result, dict) or not isinstance(result.get("pins"), list):
        raise ValueError("Input must be a refs JSON object containing pins.")
    pins = result["pins"]
    by_id = {p.get("candidate_id"): p for p in pins}
    chosen: list[dict[str, Any]] = []
    for candidate_id in dict.fromkeys(args.select_candidate):
        pin = by_id.get(candidate_id)
        if not pin:
            raise ValueError(f"Unknown candidate_id: {candidate_id}")
        if not pin.get("local_path"):
            raise ValueError(f"Candidate has no downloaded preview: {candidate_id}. Review it before selection.")
        pin["preview_validation"] = validate_preview(Path(pin["local_path"]))
        chosen.append(pin)
    for pin in pins:
        pin["display_index"] = None
    for index, pin in enumerate(chosen, 1):
        pin["display_index"] = index
    sheet = None
    if args.contact_sheet:
        sheet_path = str(Path(args.download_dir or Path(args.input).parent) / "selected-contact-sheet.jpg") if args.contact_sheet == "__auto__" else args.contact_sheet
        sheet = build_contact_sheet(chosen, sheet_path, index_field="display_index")
    result.update({
        "schema_version": 2,
        "stage": "selected_for_display",
        "display_candidates": [pool_entry(p, "display_index") for p in chosen],
        "display_count": len(chosen),
        "display_contact_sheet": sheet,
        "selection_method": "explicit_candidate_ids_after_visual_review",
        "visual_diversity": "not_automatically_assessed",
    })
    return result


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if args.workers < 1 or args.timeout <= 0 or any(value is not None and value < 0 for value in (args.limit, args.per_query_limit, args.download_count, args.review_count, args.retries)):
        print("Workers and timeout must be positive; counts and retries must be nonnegative.", file=sys.stderr)
        return 2

    if args.validate_plan:
        try:
            if not args.query_plan or args.input or args.download_original or args.select_candidate:
                raise ValueError("--validate-plan requires --query-plan and cannot run with selection or original-download modes.")
            query_plan, brief = load_search_plan(args.queries, args.query_plan, require_brief=args.require_brief)
            if not query_plan:
                raise ValueError("Provide at least one planned query.")
        except (OSError, ValueError) as exc:
            print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False))
            return 2
        print(json.dumps({
            "valid": True,
            "strict_brief": args.require_brief,
            "query_count": len(query_plan),
            "directions": sorted({p["direction"] for p in query_plan if p.get("direction")}),
            "scope": brief.get("scope") if isinstance(brief, dict) else None,
            "assessment": "structure_only; aesthetics_and_user_evidence_not_authenticated",
        }, ensure_ascii=False))
        return 0

    if args.input:
        try:
            write_result(select_local_candidates(args), args.output)
        except (OSError, ValueError, KeyError) as exc:
            print(f"Local selection failed: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.select_candidate:
        print("--select-candidate requires --input.", file=sys.stderr)
        return 2

    if args.download_original:
        if not args.output:
            print("--download-original requires --output as the destination file path.", file=sys.stderr)
            return 2
        try:
            result = download_original(args.download_original, args.output)
        except Exception as exc:
            print(f"Original download failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"success": True, **result}, ensure_ascii=False, indent=2))
        return 0

    try:
        query_plan, search_brief = load_search_plan(args.queries, args.query_plan, require_brief=args.require_brief)
    except (OSError, ValueError) as exc:
        print(f"Invalid query plan: {exc}", file=sys.stderr)
        return 2
    queries = list(dict.fromkeys(p["query"] for p in query_plan))
    if not queries:
        print("Provide queries, --query-plan, --input selection, or --download-original.", file=sys.stderr)
        return 2
    if args.cursor and len(queries) > 1:
        print("--cursor is only valid with a single query.", file=sys.stderr)
        return 2
    if args.contact_sheet and not args.download_dir:
        print("Search --contact-sheet requires --download-dir.", file=sys.stderr)
        return 2

    api_key = load_api_key(args.api_key)
    if not api_key:
        print(
            f"Missing API key. Set SCRAPECREATORS_API_KEY, pass --api-key, or store it in macOS Keychain service '{KEYCHAIN_SERVICE}'.",
            file=sys.stderr,
        )
        return 2

    query_results: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, str]] = []

    def run_query(query: str) -> list[dict[str, Any]]:
        payload = search_pinterest(
            query,
            api_key,
            cursor=args.cursor,
            trim=args.trim,
            timeout=args.timeout,
            retries=max(args.retries, 0),
        )
        return normalize_pins(payload, query=query, limit=args.per_query_limit)

    with ThreadPoolExecutor(max_workers=min(args.workers, len(queries))) as pool:
        futures = {pool.submit(run_query, q): q for q in queries}
        for future in as_completed(futures):
            query = futures[future]
            try:
                query_results[query] = future.result()
            except PinterestSearchError as exc:
                errors.append({"query": query, "error": str(exc).replace(api_key, "[redacted]")[:300]})

    if not any(query_results.values()) and errors:
        print(f"All Pinterest searches failed: {json.dumps(errors, ensure_ascii=False)}", file=sys.stderr)
        return 1

    pins, dedup_summary = merge_candidates(query_results, query_plan, args.limit)

    contact_sheet_path: str | None = None
    target = args.review_count if args.review_count is not None else args.download_count
    if args.download_dir and pins:
        download_previews(pins, args.download_dir, target, args.workers)
        if args.contact_sheet:
            sheet_out = (
                str(Path(args.download_dir) / "contact-sheet.jpg")
                if args.contact_sheet == "__auto__"
                else args.contact_sheet
            )
            contact_sheet_path = build_contact_sheet(pins, sheet_out)

    downloaded = [p for p in pins if p.get("local_path")]
    failed_count = sum(p.get("download_status") == "failed" for p in pins)
    decoded_count = sum(p.get("preview_validation") == "decoded" for p in downloaded)
    source_groups: dict[str, list[str]] = {}
    for pin in pins:
        for source in pin["canonical_source_links"]:
            source_groups.setdefault(source, []).append(pin["candidate_id"])
    normalized = {
        "schema_version": 2,
        "success": True,
        "stage": "awaiting_visual_review" if decoded_count else "review_pool_incomplete",
        "queries": queries,
        "query_plan": query_plan,
        "brief": search_brief,
        "count": len(pins),
        "contact_sheet": contact_sheet_path,
        "pins": pins,
        "review_candidates": [pool_entry(p, "review_index") for p in downloaded],
        "review_preview_count": len(downloaded),
        "decoded_preview_count": decoded_count,
        "display_candidates": [],
        "display_count": 0,
        "display_contact_sheet": None,
        "visual_diversity": "not_automatically_assessed",
        "deduplication": dedup_summary,
        "source_groups": {source: ids for source, ids in source_groups.items() if len(ids) > 1},
        "download_summary": {
            "requested_count": target if args.download_dir else 0,
            "attempted_count": len(downloaded) + failed_count,
            "downloaded_count": len(downloaded),
            "decoded_count": decoded_count,
            "unverified_count": len(downloaded) - decoded_count,
            "failed_count": failed_count,
            "shortfall_count": max(0, target - len(downloaded)) if args.download_dir else 0,
            "remaining_pool_count": sum(p.get("download_status") == "not_requested" for p in pins),
        },
        "errors": sorted(errors, key=lambda e: e["query"]),
    }
    write_result(normalized, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
