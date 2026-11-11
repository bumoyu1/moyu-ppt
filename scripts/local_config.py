#!/usr/bin/env python3
"""Per-user settings and credentials for moyu-ppt; no network.

Everything lives outside the skill directory so that upgrading or re-cloning the
skill never touches it and nothing personal can be committed by accident:

    $MOYU_PPT_CONFIG_DIR, else $XDG_CONFIG_HOME/moyu-ppt,
    else ~/.config/moyu-ppt
        config.json        platform, image channel, model names (no secrets)
        credentials.json   API keys, file mode 600

Key values are never printed by this module.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

SCRIPT_INTERFACE = "internal-module"

CONFIG_ENV = "MOYU_PPT_CONFIG_DIR"
PLATFORMS = {
    "doubao": "豆包工作",
    "codex": "Codex",
    "workbuddy": "WorkBuddy",
    "claude-code": "Claude Code",
    "other": "其他",
}
# Platforms whose own image tool is used directly, with no external key.
BUILT_IN_IMAGE_PLATFORMS = {"doubao", "codex"}
IMAGE_CHOICES = ("built-in", "gpt-image-2", "seedream", "apiyi")
# name -> (environment variable, macOS Keychain service, what it is for)
CREDENTIALS = {
    "scrapecreators": ("SCRAPECREATORS_API_KEY", "scrapecreators_pinterest_api_key", "灵感检索（Pinterest）"),
    "zenmux": ("ZENMUX_API_KEY", "zenmux_api_key", "外部生图：gpt-image 系列（ZenMux）"),
    "ark": ("ARK_API_KEY", "ark_api_key", "外部生图：Seedream（火山方舟）"),
    "apiyi": ("APIYI_API_KEY", "apiyi_api_key", "外部生图：gpt-image 系列（APIYI 中转）"),
}
CHANNEL_CREDENTIAL = {"gpt-image-2": "zenmux", "seedream": "ark", "apiyi": "apiyi"}


def config_dir() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "moyu-ppt"


def _read(name: str) -> dict:
    path = config_dir() / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(name: str, data: dict, private: bool = False) -> Path:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        if private:
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def load_config() -> dict:
    return _read("config.json")


def save_config(data: dict) -> Path:
    return _write("config.json", data)


def read_credential(name: str) -> str | None:
    """Return a locally stored key, or None. Environment and Keychain are checked by callers."""
    value = _read("credentials.json").get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def write_credential(name: str, value: str) -> Path:
    if name not in CREDENTIALS:
        raise ValueError(f"Unknown credential: {name}")
    value = (value or "").strip()
    if not value or any(ch.isspace() for ch in value):
        raise ValueError("The key is empty or contains spaces; paste the key only.")
    data = _read("credentials.json")
    data[name] = value
    return _write("credentials.json", data, private=True)


def remove_credential(name: str) -> bool:
    data = _read("credentials.json")
    if name not in data:
        return False
    del data[name]
    _write("credentials.json", data, private=True)
    return True


def configured_model(channel: str) -> str | None:
    models = load_config().get("models")
    value = models.get(channel) if isinstance(models, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None
