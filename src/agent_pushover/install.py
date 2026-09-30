"""Reversible installer. Merge owned hooks; never replace unrelated settings."""
from __future__ import annotations

import copy
import getpass
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

from .config import DEFAULTS, atomic_write, home_path, load_config, load_credentials, private_dir, read_object, write_json
from .engine import lock
from .pushover import send, validate

EVENTS = {
    "claude": {
        "Stop": None, "StopFailure": None, "PermissionRequest": None,
        "Notification": None, "PreToolUse": "AskUserQuestion|ExitPlanMode",
        "PostToolUseFailure": None, "PostToolUse": None, "PermissionDenied": None,
        "Elicitation": None, "SessionStart": None, "UserPromptSubmit": None, "SessionEnd": None,
    },
    "codex": {
        "Stop": None, "PermissionRequest": None, "PreToolUse": None, "PostToolUse": None,
        "SessionStart": None, "UserPromptSubmit": None, "SessionEnd": None,
    },
}


def json_bytes(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode()


def hook_document(original: dict, platform: str, command: str, old_commands=()) -> dict:
    result = remove_hooks(original, set(old_commands) | {command})
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Existing hooks field is not an object")
    for event, matcher in EVENTS[platform].items():
        entry = {"hooks": [{"type": "command", "command": command,
                            "timeout": 3 if event == "SessionEnd" else 5}]}
        if matcher is not None:
            entry["matcher"] = matcher
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"Existing hooks.{event} is not an array")
        groups.append(entry)
    return result


def remove_hooks(original: dict, owned: set[str]) -> dict:
    result = copy.deepcopy(original)
    hooks = result.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Existing hooks field is not an object")
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            raise ValueError(f"Existing hooks.{event} is not an array")
        new_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"Malformed hook group in {event}; leaving the file unchanged")
            remaining = [h for h in group["hooks"] if not (
                isinstance(h, dict) and h.get("type") == "command" and h.get("command") in owned)]
            if remaining == group["hooks"]:
                new_groups.append(group)
            elif remaining:
                new_groups.append({**group, "hooks": remaining})
        if new_groups:
            hooks[event] = new_groups
        elif groups:  # Leave pre-existing empty arrays alone.
            del hooks[event]
    return result


def target_path(platform: str) -> Path:
    if platform == "claude":
        directory = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        return directory.expanduser().absolute() / "settings.json"
    directory = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return directory.expanduser().absolute() / "hooks.json"


def snapshot(path: Path) -> bytes | None:
    if path.is_symlink():
        raise ValueError(f"Refusing a symlink: {path}. Use a non-symlink config location.")
    return path.read_bytes() if path.exists() else None


def launcher(home: Path, python: str, suffix: list[str]) -> bytes:
    command = [python, "-I", str(home / "runtime" / "entry.py"), "--home", str(home), *suffix]
    return ("#!/bin/sh\n# agent-pushover managed launcher\nexec " + shlex.join(command) + ' "$@"\n').encode()


def check_codex_settings(path: Path) -> list[str]:
    config_file = path.parent / "config.toml"
    warnings = []
    if config_file.exists():
        config = tomllib.loads(config_file.read_text(encoding="utf-8"))
        features = config.get("features", {})
        if features.get("hooks", features.get("codex_hooks")) is False:
            raise ValueError("Codex hooks are explicitly disabled. Enable them intentionally with "
                             "`codex features enable hooks` before installing; no config was changed.")
        if config.get("hooks"):
            warnings.append("Codex already has inline [hooks]; its CLI may warn about two hook sources. "
                            "Both are preserved and merged by Codex.")
    return warnings


def install(args) -> int:
    if os.name != "posix":
        raise ValueError("This installer supports macOS, Linux, and WSL; native Windows is not supported.")
    home = home_path(args.install_home or args.home)
    if home.resolve() in (Path.home().resolve(), Path("/")):
        raise ValueError("Choose a dedicated agent-pushover directory, not your home or filesystem root")
    private_dir(home)
    source = Path(args.source_dir).resolve() / "src"
    if not (source / "agent_pushover" / "cli.py").is_file() or not (source / "entry.py").is_file():
        raise ValueError("Run install.sh from a complete copy of the repository")
    platforms = list(EVENTS) if args.platform == "all" else [args.platform]
    bin_dir = Path(args.bin_dir or Path.home() / ".local" / "bin").expanduser().absolute()
    python = str(Path(sys.executable).absolute())
    warnings = []
    with lock(home / "install.lock"):
        old_manifest = read_object(home / "install-manifest.json")
        manifest = copy.deepcopy(old_manifest) or {"schema": 1, "platforms": {}, "launchers": {}}
        if manifest.get("schema") != 1:
            raise ValueError("Unrecognized installation manifest")
        originals: dict[Path, bytes | None] = {}
        changes: dict[Path, tuple[bytes | None, int]] = {}
        def plan(path: Path, content: bytes | None, mode: int = 0o600):
            originals[path] = snapshot(path)
            changes[path] = (content, mode)

        # Parse and validate every agent config before asking for credentials or changing files.
        for platform in platforms:
            old = manifest["platforms"].get(platform, {})
            path = target_path(platform)
            if old.get("path") and Path(old["path"]) != path:
                raise ValueError(f"{platform} was installed at {old['path']}; uninstall before changing its config directory")
            original = read_object(path)
            if platform == "codex":
                warnings.extend(check_codex_settings(path))
            if platform == "claude" and original.get("disableAllHooks") is True:
                raise ValueError("Claude hooks are explicitly disabled; enable them intentionally before installing")
            command = shlex.join([python, "-I", str(home / "runtime" / "entry.py"),
                                  "--home", str(home), "hook", "--platform", platform])
            updated = hook_document(original, platform, command, old.get("commands", []))
            plan(path, json_bytes(updated))
            manifest["platforms"][platform] = {
                "path": str(path), "file_preexisted": old.get("file_preexisted", path.exists()),
                "hooks_preexisted": old.get("hooks_preexisted", "hooks" in original), "commands": [command],
            }
        for name, suffix in {"agent-pushover": [], **{p + "-pushover": ["run", p] for p in platforms}}.items():
            path = bin_dir / name
            current = snapshot(path)
            expected = manifest["launchers"].get(str(path))
            if current is not None and hashlib.sha256(current).hexdigest() != expected:
                raise ValueError(f"Refusing to overwrite an unowned or locally modified launcher: {path}")
            content = launcher(home, python, suffix)
            plan(path, content, 0o755)
            manifest["launchers"][str(path)] = hashlib.sha256(content).hexdigest()

        config = load_config(home)
        if args.env_only and not (os.environ.get("PUSHOVER_USER_KEY") and os.environ.get("PUSHOVER_APP_TOKEN")):
            raise ValueError("--env-only requires both PUSHOVER_USER_KEY and PUSHOVER_APP_TOKEN in the environment")
        saved = read_object(home / "credentials.json")
        credentials = {"user": os.environ.get("PUSHOVER_USER_KEY") or saved.get("user", ""),
                       "token": os.environ.get("PUSHOVER_APP_TOKEN") or saved.get("token", "")}
        for key, label in (("user", "Pushover user/group key"), ("token", "Pushover application API token")):
            if not credentials[key]:
                if args.non_interactive or not sys.stdin.isatty():
                    raise ValueError("Missing credentials. Set PUSHOVER_USER_KEY and PUSHOVER_APP_TOKEN, "
                                     "or run the installer interactively")
                credentials[key] = getpass.getpass(label + " (hidden): ").strip()
        import re
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9]{30}", v) for v in credentials.values()):
            raise ValueError("Pushover user key and app token must each have 30 alphanumeric characters")
        if not args.skip_validate:
            validate(credentials, config["pushover"]["timeout_seconds"])
        if args.env_only:
            # Prevent a stale persisted credential from overriding an intentionally env-only install later.
            plan(home / "credentials.json", None)
        else:
            plan(home / "credentials.json", json_bytes(credentials))
        plan(home / "config.json", json_bytes(config))
        plan(home / "disabled", None)
        manifest.update({"home": str(home), "python": python, "bin_dir": str(bin_dir), "version": "1.0.0"})
        plan(home / "install-manifest.json", json_bytes(manifest))

        staged = Path(tempfile.mkdtemp(prefix="runtime-stage-", dir=home))
        previous_runtime = home / ("runtime-old-" + str(time.time_ns()))
        runtime = home / "runtime"
        changed: list[Path] = []
        moved_old = False
        activated = False
        try:
            shutil.copytree(source, staged, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
            if runtime.is_symlink():
                raise ValueError("Refusing a symlink runtime directory")
            # Backups are byte-for-byte, private, and are not used to stomp later user edits on uninstall.
            backup_dir = home / "backups"
            private_dir(backup_dir)
            for platform in platforms:
                path = Path(manifest["platforms"][platform]["path"])
                if originals[path] is not None and originals[path] != changes[path][0]:
                    atomic_write(backup_dir / f"{platform}-{time.time_ns()}.json", originals[path])
            for path, before in originals.items():
                if snapshot(path) != before:
                    raise ValueError(f"File changed during installation; retry after closing its editor: {path}")
            if runtime.exists():
                os.replace(runtime, previous_runtime)
                moved_old = True
            os.replace(staged, runtime)
            activated = True
            for path, (content, mode) in changes.items():
                if snapshot(path) != originals[path]:
                    raise ValueError(f"Concurrent edit detected: {path}")
                if content is None:
                    if path.exists():
                        path.unlink()
                else:
                    atomic_write(path, content, mode)
                changed.append(path)
        except Exception:
            # Roll back only files still equal to what this transaction wrote.
            for path in reversed(changed):
                installed = changes[path][0]
                if snapshot(path) == installed:
                    if originals[path] is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write(path, originals[path], changes[path][1])
            if activated:
                shutil.rmtree(runtime)
            if moved_old:
                os.replace(previous_runtime, runtime)
            raise
        finally:
            if staged.exists():
                shutil.rmtree(staged)
        if previous_runtime.exists():
            shutil.rmtree(previous_runtime)

    print(f"Installed agent-pushover into {home}")
    for platform in platforms:
        print(f"  {platform}: {manifest['platforms'][platform]['path']}")
    print(f"  Diagnostics: {shlex.quote(str(bin_dir / 'agent-pushover'))} doctor")
    print("Restart existing Claude Code/Codex processes to load the installed hooks.")
    if "codex" in platforms:
        print("ACTION REQUIRED: open /hooks in Codex and review/trust these new hooks. No trust bypass was configured.")
        print(f"Use {bin_dir / 'codex-pushover'} for terminal-visible rate-limit/error detection.")
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"Note: {bin_dir} is not on PATH. Hooks work anyway; launch the wrappers using their absolute paths.")
    if args.env_only:
        print("Credentials were NOT saved; both PUSHOVER_* variables must be inherited by every agent session.")
    for warning in warnings:
        print("Warning:", warning)
    if not args.skip_test:
        try:
            send(credentials, {"title": "Agent Pushover installed", "message": "Test notification: the sender is configured. "
                               "Hook activation still requires restarting the agents and reviewing Codex hooks.", "priority": 0}, config)
            print("Pushover accepted the installation test notification.")
        except Exception as exc:
            print(f"Installation succeeded, but the test send failed ({type(exc).__name__}). Run agent-pushover test.")
    return 0


def uninstall(home: Path, purge: bool = False) -> int:
    private_dir(home)
    with lock(home / "install.lock"):
        manifest = read_object(home / "install-manifest.json", missing=False)
        if manifest.get("schema") != 1 or Path(manifest.get("home", "")) != home:
            raise ValueError("Installation manifest does not match this directory")
        planned = {}
        for platform, info in manifest["platforms"].items():
            path = Path(info["path"])
            before = snapshot(path)
            if before is None:
                continue
            original = read_object(path)
            updated = remove_hooks(original, set(info["commands"]))
            if not updated.get("hooks") and not info["hooks_preexisted"]:
                updated.pop("hooks", None)
            after = None if not updated and not info["file_preexisted"] else json_bytes(updated)
            planned[path] = (before, after)
        # Validate launchers before any mutation; never remove edited/unowned files.
        launchers = []
        for name, expected in manifest["launchers"].items():
            path = Path(name)
            content = snapshot(path)
            if content is not None:
                if hashlib.sha256(content).hexdigest() != expected:
                    print(f"Leaving locally modified launcher untouched: {path}")
                else:
                    launchers.append(path)
        atomic_write(home / "disabled", b"uninstalled\n")
        for path, (before, after) in planned.items():
            if snapshot(path) != before:
                raise ValueError(f"Concurrent config edit detected; disabled integration but left {path} unchanged")
            if after is None:
                path.unlink()
            else:
                atomic_write(path, after)
        # The sender notices 'disabled', drops pending jobs, and releases the lock.
        with lock(home / "worker.lock"):
            for path in launchers:
                path.unlink(missing_ok=True)
            runtime = home / "runtime"
            if runtime.is_symlink():
                raise ValueError("Refusing to remove a symlink runtime")
            if runtime.exists():
                shutil.rmtree(runtime)
            (home / "install-manifest.json").unlink(missing_ok=True)
            if purge:
                for name in ("credentials.json", "config.json", "state.sqlite3", "state.sqlite3-wal",
                             "state.sqlite3-shm", "events.log", "events.log.1"):
                    (home / name).unlink(missing_ok=True)
                backups = home / "backups"
                if backups.exists() and not backups.is_symlink():
                    shutil.rmtree(backups)
    print("Removed installed hooks and unmodified launchers. Unrelated agent settings were preserved.")
    if purge:
        print("Removed credentials, queue, configuration, logs, and backups. A disabled marker remains for old sessions.")
    else:
        print(f"Kept private credentials/configuration/backups in {home}; --purge removes them during uninstall.")
    print("Restart any running agent sessions; they may have cached the old hook commands.")
    return 0
