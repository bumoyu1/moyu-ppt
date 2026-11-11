#!/usr/bin/env python3
"""Inspect image-backed slides and assemble a verified, full-bleed PPTX.

All manifest paths are relative to the deck working directory. OCR is local,
optional evidence; a clean OCR comparison never substitutes for visual review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

import text_lock


def ratio(value):
    a, b = map(float, re.split(r"[:x]", str(value)))
    if not math.isfinite(a) or not math.isfinite(b) or a <= 0 or b <= 0:
        raise ValueError("Aspect ratio must be finite and positive")
    return a / b


def load_jobs(paths):
    jobs = []
    for path in paths:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("Manifest must be a jobs array")
        jobs.extend(data)
    return validate_jobs(jobs)


def validate_jobs(jobs):
    """Validate page identity before sorting; never coerce fractional pages."""
    checked = []
    for original in jobs:
        if not isinstance(original, dict):
            raise ValueError("Every job must be an object")
        job = dict(original)
        if not isinstance(job.get("output"), str) or not job["output"].strip():
            raise ValueError("Every job needs output")
        if "page" not in job:
            match = re.search(r"p(\d+)", Path(job["output"]).stem, re.I)
            if not match:
                raise ValueError("Every slide needs a page number")
            job["page"] = int(match.group(1))
        page = job["page"]
        if isinstance(page, bool) or not re.fullmatch(r"[0-9]+", str(page)) or int(page) < 1:
            raise ValueError("Page numbers must be positive integers")
        job["page"] = int(page)
        checked.append(job)
    pages = [j["page"] for j in checked]
    if len(set(pages)) != len(pages) or len({str(Path(j['output']).resolve()) for j in checked}) != len(checked):
        raise ValueError("Duplicate page numbers or output files")
    if sorted(pages) != list(range(1, len(pages) + 1)):
        raise ValueError("Delivery pages must be complete and contiguous from 1")
    if not checked:
        raise ValueError("Delivery needs at least one slide")
    return sorted(checked, key=lambda j: j["page"])


def expected_copy(job):
    text = job.get("prompt") or ""
    if job.get("prompt_file"):
        text += Path(job["prompt_file"]).read_text(encoding="utf-8")
    # Match the extractor's accepted visible-copy syntax. Position metadata is
    # optional for copy extraction and never part of the expected image text.
    return [value.strip() for value in re.findall(
        r"^[ \t]*可见文字[ \t]*=[ \t]*([^；;\r\n]*)", text, re.M)]


def local_ocr(path):
    swift = shutil.which("swift")
    if sys.platform != "darwin" or not swift:
        return {"status": "unavailable", "reason": "macOS Vision/Swift unavailable"}
    script = Path(__file__).with_name("ocr_macos.swift")
    try:
        proc = subprocess.run([swift, str(script), str(path.resolve())], capture_output=True, text=True, timeout=60)
        if proc.returncode:
            return {"status": "failed", "reason": proc.stderr[-500:]}
        return {"status": "completed", **json.loads(proc.stdout)}
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"status": "failed", "reason": str(exc)[:300]}


def normalized(text):
    # Preserve punctuation, numbers, signs and script; only ignore spacing.
    return re.sub(r"\s+", "", text)


def review_evidence(job, review, image_sha256, extra_required=()):
    """A review applies to this exact image and states its checked dimensions."""
    required = {"text", "layout", "style", *extra_required}
    if any(isinstance(ref, dict) and ref.get("role") == "brand" for ref in job.get("references", [])):
        required.add("brand")
    if job.get("qr_required") or job.get("qr_payload"):
        required.add("qr")
    if not isinstance(review, dict):
        return {"status": "pending", "required_checks": sorted(required)}
    checks = review.get("checks")
    checked = {s.strip() for s in checks if isinstance(s, str) and s.strip()} if isinstance(checks, list) else set()
    try:
        datetime.fromisoformat(str(review.get("reviewed_at", "")).replace("Z", "+00:00"))
        timestamp_ok = True
    except ValueError:
        timestamp_ok = False
    valid = (review.get("status") == "passed" and isinstance(review.get("reviewer"), str)
             and bool(review["reviewer"].strip()) and timestamp_ok and required <= checked
             and review.get("image_sha256") == image_sha256)
    if valid:
        return {**review, "required_checks": sorted(required)}
    return {"status": "pending", "required_checks": sorted(required),
            "reason": "Review must identify the reviewer, ISO date, current image_sha256, and all required checks."}


def qr_evidence(job, ocr_result, review):
    """Require decoded contents, not merely a checked 'qr' review category."""
    if not (job.get("qr_required") or job.get("qr_payload")):
        return {"status": "not_required"}
    target = job.get("qr_payload")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        return {"status": "failed", "reason": "invalid_qr_target"}
    evidence = []
    payloads = ocr_result.get("qr_payloads")
    if (ocr_result.get("status") == "completed" and isinstance(payloads, list) and payloads
            and all(isinstance(p, str) and p.strip() for p in payloads)):
        evidence.append({"source": "local_ocr", "method": "macOS Vision QR decoder", "payloads": payloads})
    # The outer review binds this record to an identified reviewer and current image hash.
    manual = review.get("qr_evidence")
    if review.get("status") == "passed" and isinstance(manual, dict):
        method, payload = manual.get("method"), manual.get("payload")
        if isinstance(method, str) and method.strip() and isinstance(payload, str) and payload.strip():
            evidence.append({"source": "review_notes", "method": method.strip(), "payloads": [payload]})
    if not evidence:
        return {"status": "pending", "reason": "qr_scan_missing_evidence", "expected_payload": target}
    # Conflicting decoded evidence needs resolution; a checkbox cannot waive a wrong payload.
    if target is not None and any(target not in item["payloads"] for item in evidence):
        return {"status": "failed", "reason": "qr_payload_mismatch", "expected_payload": target, "evidence": evidence}
    return {"status": "passed", "expected_payload": target,
            "target_match": True if target is not None else None, "evidence": evidence}


def text_lock_accepted(review, image_sha256):
    """Remaining drift may ship only with a recorded decision bound to this exact image."""
    accepted = review.get("text_lock_accepted") if isinstance(review, dict) else None
    return (isinstance(accepted, dict) and review.get("image_sha256") == image_sha256
            and all(isinstance(accepted.get(key), str) and accepted[key].strip() for key in ("by", "note")))


def inspect_jobs(jobs, aspect_ratio, ocr="auto", review_notes=None, check_text_locks=True, lock_tolerances=None):
    jobs = validate_jobs(jobs)
    deck_ratio = ratio(aspect_ratio)
    pages = []
    notes = review_notes or {}
    if not isinstance(notes, dict):
        raise ValueError("Review notes must be keyed by page number")
    for job in jobs:
        path = Path(job["output"])
        page = {"page": job["page"], "path": str(path), "errors": [], "warnings": []}
        try:
            with Image.open(path) as img:
                img.verify()
            with Image.open(path) as img:
                img.load()
                w, h = img.size
                page["size"] = [w, h]
            desired = ratio(job.get("aspect_ratio", aspect_ratio))
            if abs(w / h - desired) / desired > .01:
                page["errors"].append("aspect_ratio_mismatch")
            if abs(w / h - deck_ratio) / deck_ratio > .01:
                page["errors"].append("deck_aspect_ratio_mismatch")
            page["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            copy = expected_copy(job)
            page["expected_text"] = copy
            result = local_ocr(path) if ocr == "auto" else {"status": "not_run"}
            page["ocr"] = result
            if result["status"] == "completed":
                observed = normalized("".join(x["text"] for x in result["lines"]))
                missing = [s for s in copy if normalized(s) not in observed]
                page["text_review_candidates"] = missing
                if missing:
                    page["warnings"].append("ocr_difference_requires_visual_review")
            else:
                page["warnings"].append("text_check_requires_visual_review")
            extra_required = set()
            review = notes.get(str(job["page"]), {})
            if check_text_locks and job.get("text_locks"):
                # Same-group lines must share one size and position. Drift blocks delivery until the
                # page is redrawn or the remaining difference is accepted on record for this image.
                locked = text_lock.measure(path, job["text_locks"], **(lock_tolerances or {}))
                page["text_lock"] = locked
                statuses = {item["status"] for item in locked["items"]}
                if "drift" in statuses:
                    if text_lock_accepted(review, page["sha256"]):
                        page["warnings"].append("text_lock_drift_accepted")
                    else:
                        page["errors"].append("text_lock_drift")
                if "unlocated" in statuses:
                    page["warnings"].append("text_lock_unverified")
                    extra_required.add("text_lock")
            page["visual_review"] = review_evidence(job, review, page["sha256"], extra_required)
            page["qr_verification"] = qr_evidence(job, result, page["visual_review"])
            qr_status = page["qr_verification"]["status"]
            if qr_status in {"pending", "failed"}:
                severity = "errors" if qr_status == "failed" else "warnings"
                page[severity].append(page["qr_verification"]["reason"])
                if page["visual_review"]["status"] == "passed":
                    page["visual_review"].update(status="pending", reason="QR decoding evidence is missing or mismatched.")
        except Exception as exc:
            page["errors"].append(str(exc))
        pages.append(page)
    structural_ok = bool(pages) and all(not p["errors"] for p in pages)
    return {"schema_version": "2.0", "structural_ok": structural_ok,
            "review_complete": structural_ok and all(p.get("visual_review", {}).get("status") == "passed" for p in pages),
            "evidence_type": "local_artifact_check", "pages": pages}


def contact_sheet(jobs, destination):
    cells = []
    for j in jobs:
        with Image.open(j["output"]) as img:
            img = img.convert("RGB")
            img.thumbnail((640, 360))
            cells.append((j["page"], img.copy()))
    columns = min(3, len(cells))
    sheet = Image.new("RGB", (columns * 660 + 20, ((len(cells) + columns - 1) // columns) * 410 + 20), "#eeeeee")
    draw = ImageDraw.Draw(sheet)
    for i, (n, img) in enumerate(cells):
        x, y = 20 + i % columns * 660, 20 + i // columns * 410
        sheet.paste(img, (x, y))
        draw.text((x, y + 370), f"P{n:02d}", fill="black")
    sheet.save(destination)


def assemble(jobs, output, aspect_ratio):
    from pptx import Presentation
    from pptx.util import Inches
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    jobs = validate_jobs(jobs)
    output = Path(output)
    if output.resolve() in {Path(job["output"]).resolve() for job in jobs}:
        raise ValueError("PPTX output must not overwrite a source slide image")
    # File validity only: main() has already applied the text-lock gate with the review notes.
    inspection = inspect_jobs(jobs, aspect_ratio, ocr="none", check_text_locks=False)
    if not inspection["structural_ok"]:
        failed = [str(page["page"]) for page in inspection["pages"] if page["errors"]]
        raise ValueError("Cannot assemble invalid slide images: pages " + ", ".join(failed))
    pres = Presentation()
    pres.slide_width = Inches(13.333333)
    pres.slide_height = int(pres.slide_width / ratio(aspect_ratio))
    for job in jobs:
        slide = pres.slides.add_slide(pres.slide_layouts[6])
        slide.shapes.add_picture(job["output"], 0, 0, width=pres.slide_width, height=pres.slide_height)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".pptx", dir=output.parent)
    os.close(fd)
    try:
        pres.save(tmp)
        check = Presentation(tmp)
        if len(check.slides) != len(jobs):
            raise ValueError("PPTX page count changed")
        for slide, job in zip(check.slides, jobs):
            if len(slide.shapes) != 1 or slide.shapes[0].shape_type != MSO_SHAPE_TYPE.PICTURE:
                raise ValueError("PPTX contains extra overlays")
            shape = slide.shapes[0]
            if (shape.left, shape.top, shape.width, shape.height) != (0, 0, check.slide_width, check.slide_height):
                raise ValueError("PPTX image is not full bleed")
            if hashlib.sha256(shape.image.blob).digest() != hashlib.sha256(Path(job['output']).read_bytes()).digest():
                raise ValueError("PPTX image order/content changed")
        os.replace(tmp, output)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return str(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", action="append", required=True)
    parser.add_argument("--aspect-ratio", default="16:9")
    parser.add_argument("--expected-pages", type=int)
    parser.add_argument("--report", default="quality-report.json")
    parser.add_argument("--review-notes", help="JSON keyed by page; status/reviewer/ISO reviewed_at/image_sha256/checks list (text/layout/style, plus brand/qr when used)")
    parser.add_argument("--ocr", choices=["auto", "none"], default="auto")
    parser.add_argument("--contact-sheet", help="Optional overview PNG path")
    parser.add_argument("--output-pptx")
    parser.add_argument("--text-lock-tolerance-height", type=float, default=text_lock.TOLERANCE_HEIGHT,
                        help="Allowed ink-height deviation of locked lines, as a fraction")
    parser.add_argument("--text-lock-tolerance-position", type=float, default=text_lock.TOLERANCE_POSITION,
                        help="Allowed shift of locked lines, in percent of the canvas side")
    args = parser.parse_args(argv)
    try:
        ratio(args.aspect_ratio)
        jobs = load_jobs(args.manifest)
    except (OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    if args.expected_pages is not None and len(jobs) != args.expected_pages:
        parser.error("Manifest does not contain expected page count")
    destinations = [Path(p).resolve() for p in (args.report, args.contact_sheet, args.output_pptx) if p]
    protected = {Path(p).resolve() for p in args.manifest}
    protected.update(Path(job["output"]).resolve() for job in jobs)
    protected.update(Path(job["prompt_file"]).resolve() for job in jobs if job.get("prompt_file"))
    if args.review_notes:
        protected.add(Path(args.review_notes).resolve())
    if len(set(destinations)) != len(destinations) or any(path in protected for path in destinations):
        parser.error("Report, contact sheet and PPTX paths must be distinct and must not overwrite source inputs")
    notes = json.loads(Path(args.review_notes).read_text()) if args.review_notes else {}
    report = inspect_jobs(jobs, args.aspect_ratio, args.ocr, notes, lock_tolerances={
        "tolerance_height": args.text_lock_tolerance_height,
        "tolerance_position": args.text_lock_tolerance_position})
    if report["structural_ok"]:
        if args.contact_sheet:
            Path(args.contact_sheet).parent.mkdir(parents=True, exist_ok=True)
            contact_sheet(jobs, args.contact_sheet)
        if args.output_pptx:
            report["pptx"] = assemble(jobs, args.output_pptx, args.aspect_ratio)
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"structural_ok": report['structural_ok'], "review_complete": report['review_complete'], "pages": len(jobs), "report": str(path), "pptx": report.get('pptx')}, ensure_ascii=False))
    return 0 if report["structural_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
