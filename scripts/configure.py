#!/usr/bin/env python3
"""One-time setup for moyu-ppt: where it runs, which image model, search key.

    configure.py status                         show what is set and what is still missing
    configure.py set-platform doubao|codex|workbuddy|claude-code|other [--image built-in|gpt-image-2|seedream|apiyi]
    configure.py set-key scrapecreators|zenmux|ark|apiyi [--value KEY | --stdin]
    configure.py set-model gpt-image-2|seedream|apiyi MODEL_ID
    configure.py skip-search                    remember that Pinterest search is not configured on purpose
    configure.py remove-key NAME

Settings are stored per user, outside the skill directory (see local_config.py).
Key values are written to a private file and never printed. No network access.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_config as lc  # noqa: E402

SEARCH_KEY_URL = "https://app.scrapecreators.com/"


def key_source(name: str) -> str | None:
    """Where a key would be found by the scripts; never returns the key itself."""
    env_var, service, _ = lc.CREDENTIALS[name]
    if os.environ.get(env_var):
        return "environment"
    if lc.read_credential(name):
        return "config-file"
    if sys.platform == "darwin":
        try:
            found = subprocess.run(["security", "find-generic-password", "-s", service],
                                   capture_output=True, timeout=5).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            found = False
        if found:
            return "macos-keychain"
    return None


def build_status() -> dict:
    config = lc.load_config()
    platform = config.get("platform")
    image = config.get("image")
    keys = {name: key_source(name) for name in lc.CREDENTIALS}
    search = "configured" if keys["scrapecreators"] else ("skipped" if config.get("search") == "skipped" else "missing")
    next_steps = []
    if platform not in lc.PLATFORMS:
        next_steps.append("问用户在哪里使用（豆包工作 / WorkBuddy / Codex / 其他），然后 set-platform。")
    elif image not in lc.IMAGE_CHOICES:
        next_steps.append("确认生图方式：当前会话有内置生图工具就 set-platform … --image built-in；"
                          "没有就选 gpt-image-2、seedream 或 apiyi 并配置对应密钥。")
    elif image != "built-in" and not keys[lc.CHANNEL_CREDENTIAL[image]]:
        credential = lc.CHANNEL_CREDENTIAL[image]
        next_steps.append(f"外部生图渠道 {image} 还没有密钥：set-key {credential}（见 references/setup.md）。")
    if search == "missing":
        next_steps.append(f"引导配置灵感检索：到 {SEARCH_KEY_URL} 获取 key，然后 set-key scrapecreators；"
                          "用户暂时不配就 skip-search。")
    return {
        "configured": not next_steps,
        "platform": platform,
        "platform_label": lc.PLATFORMS.get(platform),
        "image": image,
        "models": config.get("models") or {},
        "search": search,
        "keys": keys,
        "config_dir": str(lc.config_dir()),
        "next_steps": next_steps,
    }


def cmd_status(args) -> dict:
    return build_status()


def cmd_set_platform(args) -> dict:
    config = lc.load_config()
    config["platform"] = args.platform
    if args.image:
        config["image"] = args.image
    elif args.platform in lc.BUILT_IN_IMAGE_PLATFORMS:
        config["image"] = "built-in"
    else:
        config.pop("image", None)  # must be decided for this platform
    lc.save_config(config)
    return build_status()


def cmd_set_key(args) -> dict:
    if args.value:
        value = args.value
    elif args.stdin or not sys.stdin.isatty():
        value = sys.stdin.readline()
    else:
        value = getpass.getpass(f"Paste the {args.name} key (input hidden): ")
    lc.write_credential(args.name, value)
    if args.name == "scrapecreators":
        config = lc.load_config()
        if config.pop("search", None) is not None:
            lc.save_config(config)
    status = build_status()
    status["saved"] = {"name": args.name, "stored_in": "config-file", "purpose": lc.CREDENTIALS[args.name][2]}
    return status


def cmd_remove_key(args) -> dict:
    removed = lc.remove_credential(args.name)
    status = build_status()
    status["removed"] = {"name": args.name, "was_stored": removed}
    return status


def cmd_set_model(args) -> dict:
    model = args.model.strip()
    if not model or any(ch.isspace() for ch in model):
        raise ValueError("Model id must be a nonempty identifier without spaces.")
    config = lc.load_config()
    config.setdefault("models", {})[args.channel] = model
    lc.save_config(config)
    return build_status()


def cmd_skip_search(args) -> dict:
    config = lc.load_config()
    config["search"] = "skipped"
    lc.save_config(config)
    return build_status()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status").set_defaults(func=cmd_status)
    p = sub.add_parser("set-platform")
    p.add_argument("platform", choices=sorted(lc.PLATFORMS))
    p.add_argument("--image", choices=lc.IMAGE_CHOICES)
    p.set_defaults(func=cmd_set_platform)
    p = sub.add_parser("set-key")
    p.add_argument("name", choices=sorted(lc.CREDENTIALS))
    p.add_argument("--value", help="The key. Prefer --stdin or the hidden prompt when typing it yourself.")
    p.add_argument("--stdin", action="store_true", help="Read the key from standard input.")
    p.set_defaults(func=cmd_set_key)
    p = sub.add_parser("remove-key")
    p.add_argument("name", choices=sorted(lc.CREDENTIALS))
    p.set_defaults(func=cmd_remove_key)
    p = sub.add_parser("set-model")
    p.add_argument("channel", choices=sorted(lc.CHANNEL_CREDENTIAL))
    p.add_argument("model")
    p.set_defaults(func=cmd_set_model)
    sub.add_parser("skip-search").set_defaults(func=cmd_skip_search)
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except (ValueError, OSError) as exc:
        print(f"configure: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
