#!/usr/bin/env python3
"""Generate, validate and resume PPT slide images with role-aware references.

Channels retain their configured defaults: gpt-image-2 through ZenMux, Seedream
through Ark, and apiyi (gpt-image-2.6-sunburst, quality high) through the APIYI relay.
--model, or the ZENMUX_IMAGE_MODEL / ARK_IMAGE_MODEL / APIYI_IMAGE_MODEL environment
variables, override the default model name; APIYI_IMAGE_QUALITY overrides the apiyi
quality tier. Provider capability is not presumed verified.
Pillow is required to fully decode and check images before atomic publication.
All relative paths resolve against the current deck working directory.

Manifest: a job array with prompt_file (or prompt), output, optional references
[{path, role, id}], target_image for edits, and per-job size/aspect_ratio/model.
Legacy reference paths remain accepted, but handwritten positional image numbers
in prompts are rejected: use stable IDs such as [S1], [L-cover], [T-title].

--dry-run performs credential-free preflight and prints resolved_prompt,
references and size; --export-inputs writes the same native/external input package.
Paged deck batches require workflow evidence even for dry-run/native export.
--generation-stage pilot permits only the selected representative pages;
production (default) additionally requires their recorded acceptance.
--edit requires --target-image and a distinct versioned --output.
--state-file defaults to run-state.json. Same-input successful images are reused;
accepted responses resume downloads only; uncertain submissions are never retried
implicitly. --retry-failed retries known rejections. --force explicitly authorizes
a new provider submission, including after a success or an uncertain outcome.
The ledger records local capability snapshots, hashes, status and download URLs,
never API keys, prompts or base64 request/response bodies.
"""
import argparse
import base64
import http.client
import time
import json
import mimetypes
import os
import subprocess
import sys
import urllib.request
import uuid
import hashlib
import io
import re
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from deck_runtime import (atomic_write, file_hash, json_bytes, normalize_references,
                          reference_instructions, reject_positional_references,
                          validate_image_bytes, validate_image_file)
from workflow_gate import require_workflow
import local_config

CHANNELS = {
    "gpt-image-2": {
        "model": "openai/gpt-image-2",
        "model_env": "ZENMUX_IMAGE_MODEL",
        "generations_url": "https://zenmux.ai/api/v1/images/generations",
        "edits_url": "https://zenmux.ai/api/v1/images/edits",
        "env_key": "ZENMUX_API_KEY",
        "keychain_service": "zenmux_api_key",
    },
    "seedream": {
        "model": "doubao-seedream-5-0-pro-260628",
        "model_env": "ARK_IMAGE_MODEL",
        "generations_url": "https://ark.cn-beijing.volces.com/api/v3/images/generations",
        "edits_url": None,  # references go into the native "image" array
        "env_key": "ARK_API_KEY",
        "keychain_service": "ark_api_key",
    },
    # APIYI relay, OpenAI Images API protocol. Returns b64_json only and rejects
    # response_format; edits take up to 16 "image[]" parts. quality is sent
    # explicitly because its "auto" default drifts in cost and latency.
    "apiyi": {
        "model": "gpt-image-2.6-sunburst",
        "model_env": "APIYI_IMAGE_MODEL",
        "generations_url": "https://api.apiyi.com/v1/images/generations",
        "edits_url": "https://api.apiyi.com/v1/images/edits",
        "env_key": "APIYI_API_KEY",
        "keychain_service": "apiyi_api_key",
        "quality": "high",
        "quality_env": "APIYI_IMAGE_QUALITY",
        "max_references": 16,
    },
}
TIMEOUT = 360  # synchronous image APIs; a client-side disconnect still bills

ASPECT_TO_SIZE = {
    "16:9": "1536x864",
    "21:9": "1792x768",
    "4:3": "1280x960",
    "3:4": "960x1280",
    "1:1": "1024x1024",
    "9:16": "864x1536",
}
TARGET_AREA = 1_300_000

# Seedream 5.0 Pro explicit-size ("宽x高") bounds: total pixels within
# [1280x720, 2048x2048]. 2K-level true-ratio sizes near the ceiling.
SEEDREAM_ASPECT_TO_SIZE = {
    "16:9": "2560x1440",
    "21:9": "3024x1296",
    "4:3": "2304x1728",
    "3:4": "1728x2304",
    "1:1": "2048x2048",
    "9:16": "1440x2560",
    "3:2": "2496x1664",
    "2:3": "1664x2496",
}
SEEDREAM_TARGET_AREA = 3_900_000
SEEDREAM_MAX_AREA = 4_194_304  # 2048x2048, 5.0 Pro ceiling
SEEDREAM_MIN_AREA = 921_600    # 1280x720, 5.0 Pro floor
SEEDREAM_MAX_REFERENCES = 10   # 5.0 Pro limit (Lite/4.5/4.0 allow 14)


def resolve_api_key(cli_key, channel):
    cfg = CHANNELS[channel]
    if cli_key:
        return cli_key
    env_key = os.environ.get(cfg["env_key"])
    if env_key:
        return env_key
    credential = local_config.CHANNEL_CREDENTIAL.get(channel)
    stored = local_config.read_credential(credential) if credential else None
    if stored:
        return stored
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", cfg["keychain_service"], "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    sys.exit(
        f"No API key for channel '{channel}'. Run configure.py set-key "
        f"{local_config.CHANNEL_CREDENTIAL.get(channel, channel)}, set {cfg['env_key']}, pass --api-key, "
        f"or store it in Keychain service '{cfg['keychain_service']}'."
    )


def default_model(channel):
    """Channel default model: *_IMAGE_MODEL env var, then configure.py set-model, then built-in."""
    cfg = CHANNELS[channel]
    return ((os.environ.get(cfg["model_env"]) or "").strip()
            or local_config.configured_model(channel) or cfg["model"])


def read_prompt(job, prefix_text=""):
    if job.get("prompt"):
        prompt = job["prompt"]
    elif job.get("prompt_file"):
        prompt = Path(job["prompt_file"]).read_bytes().decode("utf-8")
    else:
        raise ValueError("Job needs 'prompt' or 'prompt_file'.")
    if job.get("prefix_file"):
        prefix_text = Path(job["prefix_file"]).read_bytes().decode("utf-8")
    return f"{prefix_text}\n\n{prompt}" if prefix_text else prompt


def resolve_size(aspect_ratio, size, channel="gpt-image-2"):
    if size:
        return size
    table = SEEDREAM_ASPECT_TO_SIZE if channel == "seedream" else ASPECT_TO_SIZE
    if aspect_ratio in table:
        return table[aspect_ratio]
    try:
        w_ratio, h_ratio = (float(part) for part in aspect_ratio.split(":"))
        ratio = w_ratio / h_ratio
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"Invalid aspect ratio: {aspect_ratio}") from exc
    if not 0.05 <= ratio <= 20:
        raise ValueError("Aspect ratio must be positive and between 1:20 and 20:1")
    target_area = SEEDREAM_TARGET_AREA if channel == "seedream" else TARGET_AREA
    height = max(int(round((target_area / ratio) ** 0.5 / 16)) * 16, 256)
    width = max(int(round(height * ratio / 16)) * 16, 256)
    if channel == "seedream" and width * height < SEEDREAM_MIN_AREA:
        scale = (SEEDREAM_MIN_AREA / (width * height)) ** 0.5
        height = int(round(height * scale / 16)) * 16 + 16
        width = int(round(height * ratio / 16)) * 16
    if channel == "seedream" and width * height > SEEDREAM_MAX_AREA:
        raise ValueError("Computed size exceeds Seedream configured maximum area; pass a valid --size")
    return f"{width}x{height}"


def image_to_data_url(path):
    file_path = Path(path)
    mime = mimetypes.guess_type(file_path.name)[0] or "image/png"
    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def save_from_response(payload, output, expected_size=None, expected_ratio=None):
    data = payload.get("data") or []
    if not data:
        raise RuntimeError("No image in provider response")
    item = data[0]
    out = Path(output)
    if item.get("b64_json"):
        image_bytes = base64.b64decode(item["b64_json"], validate=True)
    elif item.get("url"):
        with urllib.request.urlopen(item["url"], timeout=TIMEOUT) as resp:
            image_bytes = resp.read()
    else:
        raise RuntimeError(f"Image item has neither b64_json nor url: {list(item.keys())}")
    validate_image_bytes(image_bytes, expected_size, expected_ratio)
    atomic_write(out, image_bytes)
    return str(out)


def post_json(url, api_key, body, retries=0):
    # Generation POSTs are never retried automatically: a timeout or 5xx can
    # happen after the provider accepted and charged the request.
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_multipart(url, api_key, fields, files):
    boundary = f"----moyu-ppt-{uuid.uuid4().hex}"
    parts = []
    for name, value in fields:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8")
        )
    for name, path in files:
        file_path = Path(path)
        mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        header = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
            f"filename=\"{file_path.name}\"\r\nContent-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        parts.append(header + file_path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def default_quality(channel):
    """Channel quality tier, or None when the channel sends no quality field."""
    cfg = CHANNELS[channel]
    if not cfg.get("quality"):
        return None
    return (os.environ.get(cfg.get("quality_env") or "") or "").strip() or cfg["quality"]


def configured_capabilities(channel, model):
    cfg = CHANNELS[channel]
    if channel == "seedream":
        max_references = SEEDREAM_MAX_REFERENCES
    else:
        max_references = cfg.get("max_references")
    capabilities = {"channel": channel, "model": model,
            "generations_url": cfg["generations_url"], "edits_url": cfg["edits_url"],
            "max_references": max_references,
            "capability_source": "local adapter configuration; model override is not provider-validated",
            "pipeline_version": 2}
    quality = default_quality(channel)
    if quality:
        # Only channels that send quality carry it, so existing input hashes stay stable.
        capabilities["quality"] = quality
    return capabilities


def prepare_job(job, model=None, aspect_ratio="16:9", size=None,
                prefix_text="", channel="gpt-image-2"):
    """Credential-free input package shared by external and native generation.

    All relative paths resolve from the current deck working directory. Returned
    resolved_prompt, references and size are the exact external request inputs.
    Native tools must use those fields rather than rebuilding role instructions.
    """
    if not isinstance(job, dict):
        raise ValueError("Each manifest job must be an object")
    if channel not in CHANNELS:
        raise ValueError(f"Unknown channel: {channel}")
    from PIL import Image  # required before any credential lookup or provider call
    output = job.get("output")
    if not isinstance(output, str) or not output:
        raise ValueError("Job requires a nonempty output path")
    if Path(output).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError("Output must use a PNG, JPEG or WebP extension")
    prompt = read_prompt(job, prefix_text)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Prompt must be nonempty")
    reject_positional_references(prompt)
    target = job.get("target_image")
    if job.get("edit") and not target:
        raise ValueError("Edit requires target_image / --target-image; references alone are not an edit target")
    refs = normalize_references(job.get("references") or [], target)
    if any(item["role"] == "target" for item in refs) and not target:
        raise ValueError("Declare the edit target in target_image, not only in references")
    model = job.get("model") or model or default_model(channel)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Model must be a nonempty model identifier")
    capabilities = configured_capabilities(channel, model)
    if capabilities["max_references"] is not None and len(refs) > capabilities["max_references"]:
        raise ValueError(f"Configured {channel} adapter accepts at most {capabilities['max_references']} images including target")
    requested_ratio = job.get("aspect_ratio", aspect_ratio)
    explicit_size = job.get("size", size)
    resolved_size = resolve_size(requested_ratio, explicit_size, channel)
    dimensions = re.fullmatch(r"(\d+)[xX](\d+)", resolved_size)
    if dimensions:
        width, height = map(int, dimensions.groups())
        if min(width, height) <= 0:
            raise ValueError("Image dimensions must be positive")
        if channel == "seedream" and not SEEDREAM_MIN_AREA <= width * height <= SEEDREAM_MAX_AREA:
            raise ValueError("Size is outside the configured Seedream adapter pixel range")
        expected_ratio = f"{width}:{height}"
    elif channel == "seedream" and resolved_size in {"1K", "2K"}:
        expected_ratio = requested_ratio
    else:
        raise ValueError(f"Unsupported configured size: {resolved_size}")
    for ref in refs:
        if Path(ref["path"]).resolve() == Path(output).resolve():
            raise ValueError("Output must differ from every reference and edit target; use a versioned output")
        # A tight glyph strip must not masquerade as a full-canvas size guide.
        # Pixel dimensions may differ, but its canvas ratio must match the output.
        ref["sha256"] = validate_image_file(
            ref["path"], expected_ratio=expected_ratio if ref["role"] == "size" else None)["sha256"]
    known_ids = {ref["id"] for ref in refs}
    used_ids = set(re.findall(r"\[(S\d+|S-board|SZ\d+|L-[\w-]+|T-[\w-]+|C\d+|family|target)\]", prompt))
    if used_ids - known_ids:
        raise ValueError(f"Prompt names references not uploaded: {sorted(used_ids - known_ids)}")
    binding = reference_instructions(refs)
    resolved_prompt = prompt + ("\n\n" + binding if binding else "")
    hashed = {"prompt": resolved_prompt, "size": resolved_size, "aspect_ratio": expected_ratio,
              "capabilities": capabilities,
              "references": [{key: value for key, value in ref.items() if key != "path"} for ref in refs]}
    digest = hashlib.sha256(json.dumps(hashed, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"page": job.get("page"), "prompt_file": job.get("prompt_file"), "output": output,
            "resolved_prompt": resolved_prompt, "references": refs, "target_image": target,
            "size": resolved_size, "aspect_ratio": expected_ratio, "model": model,
            "channel": channel, "input_hash": digest, "capabilities": capabilities}


def now():
    return datetime.now(timezone.utc).isoformat()


class RunState:
    """Small durable ledger; no prompts, credentials, request bodies or base64."""
    def __init__(self, path="run-state.json"):
        self.path = Path(path)
        self.guard = threading.RLock()
        self.lock_stream = None
        self.data = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_stream = self.path.with_suffix(self.path.suffix + ".lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.lock_stream.seek(0)
                self.lock_stream.write(b"0")
                self.lock_stream.flush()
                self.lock_stream.seek(0)
                msvcrt.locking(self.lock_stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.data = json.loads(self.path.read_text()) if self.path.exists() else {"schema_version": 2, "jobs": {}, "runs": []}
            if not isinstance(self.data, dict) or not isinstance(self.data.get("jobs"), dict):
                raise ValueError("Invalid run-state.json; preserve it and choose a new --state-file")
        except Exception:
            self.lock_stream.close()
            self.lock_stream = None
            raise
        return self

    def __exit__(self, *args):
        if self.lock_stream:
            self.lock_stream.close()

    def get(self, output):
        with self.guard:
            return dict(self.data["jobs"].get(str(Path(output).resolve()), {}))

    def put(self, output, record):
        with self.guard:
            self.data["jobs"][str(Path(output).resolve())] = {**record, "updated_at": now()}
            atomic_write(self.path, json_bytes(self.data))

    def start_run(self, packages):
        with self.guard:
            snapshots = []
            for package in packages:
                if package["capabilities"] not in snapshots:
                    snapshots.append(package["capabilities"])
            self.data.setdefault("runs", []).append({"started_at": now(), "capabilities": snapshots,
                "jobs": [{"output": p["output"], "input_hash": p["input_hash"],
                          "size": p["size"], "aspect_ratio": p["aspect_ratio"]} for p in packages]})
            self.data["runs"] = self.data["runs"][-50:]
            atomic_write(self.path, json_bytes(self.data))


def provider_hint(exc, api_key=None):
    """Short reason from a provider's error body, for the console only; never persisted."""
    if not isinstance(exc, urllib.error.HTTPError):
        return ""
    try:
        body = exc.read(4000).decode("utf-8", "replace")
        error = json.loads(body).get("error")
        if isinstance(error, dict):
            parts = [str(error.get(key)) for key in ("type", "code") if error.get(key)]
            message = str(error.get("message") or "")
        else:
            parts, message = [], str(error or "")
        text = " ".join(dict.fromkeys(parts)) + (": " if parts and message else "") + message
    except Exception:
        return ""
    text = " ".join(text.split())[:180]
    return text.replace(api_key, "[redacted]") if api_key and isinstance(api_key, str) else text


def safe_error(exc, api_key=None):
    # Do not persist provider error bodies, signed request URLs or echoed prompts.
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, ValueError):
        message = str(exc)[:400]
        return message.replace(api_key, "[redacted]") if api_key else message
    return type(exc).__name__


def submit_prepared(api_key, package):
    cfg = CHANNELS[package["channel"]]
    refs = package["references"]
    if package["channel"] == "seedream":
        body = {"model": package["model"], "prompt": package["resolved_prompt"],
                "size": package["size"], "response_format": "url", "output_format": "png", "watermark": False}
        if refs:
            body["image"] = [image_to_data_url(ref["path"]) for ref in refs]
        return post_json(cfg["generations_url"], api_key, body)
    quality = package["capabilities"].get("quality")
    if refs:
        fields = [("model", package["model"]), ("prompt", package["resolved_prompt"]), ("size", package["size"])]
        if quality:
            fields.append(("quality", quality))
        return post_multipart(cfg["edits_url"], api_key, fields,
            [("image[]", ref["path"]) for ref in refs])
    body = {"model": package["model"], "prompt": package["resolved_prompt"], "n": 1, "size": package["size"]}
    if quality:
        body["quality"] = quality
    return post_json(cfg["generations_url"], api_key, body)


def recover_image(package, record, state):
    """Resume only the accepted image download/validation, never a new generation."""
    pending = record.get("pending_image")
    try:
        if pending and Path(pending).is_file():
            data = Path(pending).read_bytes()
        elif record.get("download_url"):
            with urllib.request.urlopen(record["download_url"], timeout=TIMEOUT) as response:
                data = response.read()
            pending = str(Path(package["output"]).with_name("." + Path(package["output"]).name + ".pending"))
            atomic_write(pending, data)
            record["pending_image"] = pending
            state.put(package["output"], record)
        else:
            raise ValueError("Accepted response has no recoverable image; inspect state and use --force only to authorize a new request")
        info = validate_image_bytes(data, package["size"], package["aspect_ratio"])
        required_format = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}[Path(package["output"]).suffix.lower()]
        if info["format"] != required_format:
            # Normalize the encoding, never redraw or change the image content.
            from PIL import Image
            with Image.open(io.BytesIO(data)) as im:
                buffer = io.BytesIO()
                if required_format == "JPEG" and im.mode not in {"L", "RGB"}:
                    im = im.convert("RGB")
                im.save(buffer, format=required_format)
                data = buffer.getvalue()
            info = validate_image_bytes(data, package["size"], package["aspect_ratio"])
        atomic_write(package["output"], data)
        record.update(status="success", image=info)
        record.pop("error", None)
        record.pop("pending_image", None)
        state.put(package["output"], record)
        if pending:
            Path(pending).unlink(missing_ok=True)
        return package["output"]
    except Exception as exc:
        if isinstance(exc, ValueError) and record.get("download_url") and pending:
            # A transient HTML/corrupt download must not trap future recovery on
            # the same bad cache. Retry GET on the next invocation, never POST.
            Path(pending).unlink(missing_ok=True)
            record.pop("pending_image", None)
        record.update(status="accepted", error=safe_error(exc))
        state.put(package["output"], record)
        raise


# From the second time the same inputs end without an answer, say how many
# requests that makes: each may have been charged, and the user decides.
REPEATED_UNANSWERED = 2


def execute_prepared(api_key, package, state, force=False, retry_failed=False):
    output = package["output"]
    record = state.get(output)
    same = record.get("input_hash") == package["input_hash"]
    if same and not force:
        if record.get("status") == "success":
            try:
                actual = validate_image_file(output, package["size"], package["aspect_ratio"])
                if actual["sha256"] != record.get("image", {}).get("sha256"):
                    raise ValueError("Output bytes changed after validation")
                return output
            except (ValueError, OSError):
                if record.get("download_url"):
                    return recover_image(package, record, state)
                raise ValueError("Successful output is missing or changed; --force is required to regenerate")
        if record.get("status") in {"accepted"}:
            return recover_image(package, record, state)
        if record.get("status") in {"submitting", "unknown"}:
            raise ValueError("Previous submission may have been accepted. No automatic resubmission; inspect provider/state, then --force if a new request is intended")
        if record.get("status") == "failed" and not retry_failed:
            raise ValueError("Previous request was rejected; --retry-failed retries it, --force starts a new request")
    elif record and not same and not force and record.get("status") in {"submitting", "unknown", "accepted"}:
        raise ValueError("Output has an unresolved earlier request with different inputs; recover it or use a different output/--force")
    for ref in package["references"]:
        if file_hash(ref["path"]) != ref["sha256"]:
            raise ValueError(f"Reference changed after preflight: {ref['path']}; run preflight again")
    if callable(api_key):
        api_key = api_key()
    # How many times these exact inputs already ended without an answer.
    unanswered = record.get("unanswered", 1 if record.get("status") == "unknown" else 0) if same else 0
    record = {"input_hash": package["input_hash"], "output": output, "status": "submitting",
              "submitted_at": now(), "capabilities": package["capabilities"],
              "size": package["size"], "aspect_ratio": package["aspect_ratio"]}
    if unanswered:
        record["unanswered"] = unanswered
    state.put(output, record)
    try:
        payload = submit_prepared(api_key, package)
    except Exception as exc:
        rejected = isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500 and exc.code != 408
        hint = provider_hint(exc, api_key)  # shown once on the console; the ledger keeps only the code
        record.update(status="failed" if rejected else "unknown", error=safe_error(exc, api_key))
        repeated = ""
        if not rejected:
            record["unanswered"] = unanswered + 1
            if record["unanswered"] >= REPEATED_UNANSWERED:
                repeated = (f"; the same inputs have now ended without an answer {record['unanswered']} times: "
                            "tell the user the count, each may have been charged. An unchanged request can still "
                            "succeed; if only this page keeps failing, send it with fewer reference images")
        state.put(output, record)
        raise ValueError(record["error"] + (f" ({hint})" if hint else "")
                         + ("; retry with --retry-failed" if rejected else "; submission outcome unknown, not retried")
                         + repeated) from None
    # Persist acceptance before any expensive decoding/download. A malformed
    # response is not evidence the provider did not generate/charge an image.
    record["status"] = "accepted"
    state.put(output, record)
    try:
        items = payload.get("data") or []
        if not items or not isinstance(items[0], dict):
            raise ValueError("Provider accepted response contains no usable image")
        item = items[0]
        if item.get("url"):
            url = item["url"]
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                raise ValueError("Image download URL must use HTTP(S)")
            if api_key and api_key in url:
                raise ValueError("Refusing to persist a download URL containing the API key")
            record["download_url"] = url
        elif item.get("b64_json"):
            image_bytes = base64.b64decode(item["b64_json"], validate=True)
            pending = str(Path(output).with_name("." + Path(output).name + ".pending"))
            atomic_write(pending, image_bytes)
            record["pending_image"] = pending
        else:
            raise ValueError("Provider image contains neither URL nor base64")
        state.put(output, record)
        return recover_image(package, record, state)
    except Exception as exc:
        record.update(status="accepted", error=safe_error(exc, api_key))
        state.put(output, record)
        raise ValueError(record["error"] + "; generation will not be resubmitted automatically") from None


def run_job(api_key, job, model, aspect_ratio, size, prefix_text="", channel="gpt-image-2",
            state=None, force=False, retry_failed=False, state_file="run-state.json"):
    package = prepare_job(job, model, aspect_ratio, size, prefix_text, channel)
    if state is not None:
        return execute_prepared(api_key, package, state, force, retry_failed)
    with RunState(state_file) as local_state:
        local_state.start_run([package])
        return execute_prepared(api_key, package, local_state, force, retry_failed)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Preflight, generate and resume slide images. All paths resolve from deck cwd.")
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("--output")
    parser.add_argument("--batch", help="JSON job array")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--aspect-ratio", default="16:9")
    parser.add_argument("--size", help="Explicit WxH; Seedream also accepts configured 1K/2K presets")
    parser.add_argument("--channel", choices=sorted(CHANNELS), default="gpt-image-2")
    parser.add_argument("--model", help="Override model; compatibility is not presumed verified")
    parser.add_argument("--edit", action="store_true", help="Requires --target-image")
    parser.add_argument("--target-image", help="Page to edit, distinct from output")
    parser.add_argument("--reference", action="append", help="Path or JSON role object; repeatable")
    parser.add_argument("--prefix-file")
    parser.add_argument("--api-key")
    parser.add_argument("--dry-run", action="store_true", help="No keys or network: emit resolved native/external input packages")
    parser.add_argument("--export-inputs", help="Write the dry-run/native input package JSON to this path")
    parser.add_argument("--state-file", default="run-state.json", help="Durable state (default: run-state.json); no API key or base64")
    parser.add_argument("--workflow-state", default="deck-state.json", help="Actual brief, visual and pilot decisions for paged deck batches")
    parser.add_argument("--generation-stage", choices=["pilot", "production"], default="production",
                        help="Paged batches default to production; pilot permits only the selected representative pages")
    parser.add_argument("--force", action="store_true", help="Authorize a new provider request even for successful/unknown jobs")
    parser.add_argument("--retry-failed", action="store_true", help="Retry rejected requests; never resubmit accepted/unknown requests")
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.workers <= 8:
            raise ValueError("workers must be between 1 and 8")
        if args.edit and not args.target_image and not args.batch:
            raise ValueError("--edit requires --target-image; --reference is only supplemental")
        if args.batch and args.target_image:
            raise ValueError("Batch edits declare target_image per job")
        prefix = Path(args.prefix_file).read_bytes().decode("utf-8") if args.prefix_file else ""
        if args.batch:
            jobs = json.loads(Path(args.batch).read_text(encoding="utf-8"))
            if not isinstance(jobs, list):
                raise ValueError("Batch manifest must be a jobs array")
            if any(not isinstance(job, dict) for job in jobs):
                raise ValueError("Every batch job must be an object")
            if args.edit:
                jobs = [{**job, "edit": True} for job in jobs]
        else:
            refs = [json.loads(ref) if ref.lstrip().startswith("{") else ref for ref in args.reference or []]
            jobs = [{"prompt": args.prompt, "prompt_file": args.prompt_file, "output": args.output,
                     "references": refs, "target_image": args.target_image, "edit": args.edit}]
        is_deck_batch = bool(args.batch) and any("page" in job for job in jobs)
        if is_deck_batch:
            require_workflow(args.workflow_state, args.generation_stage,
                             pages=[job.get("page") for job in jobs],
                             aspect_ratios=[job.get("aspect_ratio", args.aspect_ratio) for job in jobs])
        packages = [prepare_job(job, args.model, args.aspect_ratio, args.size, prefix, args.channel) for job in jobs]
        if is_deck_batch:
            require_workflow(args.workflow_state, args.generation_stage,
                             pages=[package["page"] for package in packages],
                             aspect_ratios=[package["aspect_ratio"] for package in packages])
        outputs = [str(Path(p["output"]).resolve()) for p in packages]
        if len(set(outputs)) != len(outputs):
            raise ValueError("Batch outputs must be unique")
        for package in packages:
            if any(str(Path(ref["path"]).resolve()) in outputs for ref in package["references"]):
                raise ValueError("Batch cannot depend on outputs in the same batch; generate/accept anchors first")
        exported = {"schema_version": 2, "cwd": str(Path.cwd()), "jobs": packages}
        if args.export_inputs:
            atomic_write(args.export_inputs, json_bytes(exported))
        if args.dry_run:
            print(json.dumps(exported, ensure_ascii=False, indent=2))
            return 0
        if not packages:
            print(json.dumps({"ok": [], "failed": []}))
            return 0
        # Resolve credentials only if a worker actually needs a new submission.
        # Reuse and recovery downloads therefore work without keychain access.
        credential_guard = threading.Lock()
        resolved_key = []
        def api_key():
            with credential_guard:
                if not resolved_key:
                    resolved_key.append(resolve_api_key(args.api_key, args.channel))
                return resolved_key[0]
        results = {"ok": [], "failed": []}
        with RunState(args.state_file) as state:
            state.start_run(packages)
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(execute_prepared, api_key, package, state, args.force, args.retry_failed): package for package in packages}
                for future in as_completed(futures):
                    package = futures[future]
                    try:
                        results["ok"].append(future.result())
                    except Exception as exc:
                        results["failed"].append({"output": package["output"], "error": safe_error(exc, resolved_key[0] if resolved_key else None)})
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 1 if results["failed"] else 0
    except (ValueError, OSError, TypeError, ImportError) as exc:
        print(f"Preflight/runtime failed: {safe_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
