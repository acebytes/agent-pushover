"""Configuration, private storage, and redaction. Python standard library only."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "triggers": {
        "complete": {"enabled": True, "priority": 0, "dedupe_seconds": 5},
        "idle": {"enabled": True, "priority": 0, "dedupe_seconds": 8},
        "rate_limit": {"enabled": True, "priority": 1, "dedupe_seconds": 120},
        "error": {"enabled": True, "priority": 1, "dedupe_seconds": 30},
    },
    "privacy": {"include_details": False, "include_project": True, "include_host": True},
    "pushover": {"device": "", "sound": "", "timeout_seconds": 5},
    "codex": {"idle_after_stop_seconds": 60},
    "terminal": {"enabled": True, "extra_rate_limit_patterns": [], "extra_error_patterns": [],
                 "extra_idle_patterns": []},
    "delivery": {"max_attempts": 3, "max_pending": 500, "max_age_seconds": 3600},
}


def home_path(value: str | Path | None = None) -> Path:
    default = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "agent-pushover"
    return Path(value or os.environ.get("AGENT_PUSHOVER_HOME") or default).expanduser().absolute()


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Refusing symlink for private directory: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
        raise ValueError(f"Directory is not owned by the current user: {path}")
    path.chmod(0o700)


def atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Replace one file atomically, never follow a file symlink."""
    if path.is_symlink():
        raise ValueError(f"Refusing to replace a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_json(path: Path, obj: Any) -> None:
    atomic_write(path, (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode())


def read_object(path: Path, *, missing: bool = True) -> dict[str, Any]:
    if not path.exists() and missing:
        return {}
    if path.is_symlink():
        raise ValueError(f"Refusing to read configuration symlink: {path}")
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return obj


def merge(base: dict, overrides: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def validate_config(config: dict) -> None:
    def number(value: Any, low: float, high: float) -> bool:
        return type(value) in (int, float) and low <= value <= high

    if config.get("version") != 1:
        raise ValueError("Unsupported configuration version")
    for name in DEFAULTS["triggers"]:
        item = config["triggers"][name]
        if type(item["enabled"]) is not bool:
            raise ValueError(f"triggers.{name}.enabled must be true or false")
        if type(item["priority"]) is not int or item["priority"] not in (-2, -1, 0, 1):
            raise ValueError("Priorities must be -2, -1, 0 or 1; emergency priority 2 is not supported")
        if not number(item["dedupe_seconds"], 0, 86400):
            raise ValueError("dedupe_seconds must be between 0 and 86400")
    for key in DEFAULTS["privacy"]:
        if type(config["privacy"][key]) is not bool:
            raise ValueError(f"privacy.{key} must be true or false")
    if not number(config["pushover"]["timeout_seconds"], 1, 30):
        raise ValueError("timeout_seconds must be between 1 and 30")
    for key in ("device", "sound"):
        if not isinstance(config["pushover"][key], str):
            raise ValueError(f"pushover.{key} must be a string")
    if not number(config["codex"]["idle_after_stop_seconds"], 0, 3600):
        raise ValueError("idle_after_stop_seconds must be between 0 and 3600; 0 disables the reminder")
    if type(config["terminal"]["enabled"]) is not bool:
        raise ValueError("terminal.enabled must be true or false")
    for key in ("extra_rate_limit_patterns", "extra_error_patterns", "extra_idle_patterns"):
        patterns = config["terminal"][key]
        if not isinstance(patterns, list) or any(not isinstance(x, str) for x in patterns):
            raise ValueError(f"terminal.{key} must be a list of regex strings")
        for pattern in patterns:
            if len(pattern) > 500:
                raise ValueError("Custom regex is too long")
            re.compile(pattern)
    for key, low, high in (("max_attempts", 1, 5), ("max_pending", 1, 5000),
                           ("max_age_seconds", 60, 86400)):
        if type(config["delivery"][key]) is not int or not low <= config["delivery"][key] <= high:
            raise ValueError(f"delivery.{key} must be an integer between {low} and {high}")


def load_config(home: Path) -> dict:
    result = merge(DEFAULTS, read_object(home / "config.json"))
    try:
        validate_config(result)
    except (KeyError, TypeError) as exc:
        raise ValueError("Malformed configuration structure") from exc
    return result


def load_credentials(home: Path) -> dict[str, str]:
    saved = read_object(home / "credentials.json")
    result = {
        "token": os.environ.get("PUSHOVER_APP_TOKEN") or saved.get("token", ""),
        "user": os.environ.get("PUSHOVER_USER_KEY") or saved.get("user", ""),
    }
    if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9]{30}", v) for v in result.values()):
        raise ValueError("Pushover requires a 30-character app token and 30-character user/group key")
    return result


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Best effort, not a DLP guarantee. Details are disabled by default."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]+)\b",
                  "[redacted]", text)
    text = re.sub(r"(?i)(\b(?:api[_-]?key|token|password|secret|authorization)\b\s*[:=]\s*)"
                  r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", r"\1[redacted]", text)
    return "".join(c for c in text if c in "\n\t" or ord(c) >= 32).strip()
