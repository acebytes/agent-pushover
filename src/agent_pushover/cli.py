"""Command line interface; hook mode is always passive and fail-open."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import home_path, load_config, load_credentials, read_object, validate_config, write_json
from .engine import Store, drain, handle, lock, log
from .events import classify, render
from .pushover import DeliveryError, send


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-pushover", description=__doc__)
    p.add_argument("--home", help="Private integration directory (or AGENT_PUSHOVER_HOME)")
    p.add_argument("--version", action="version", version=__version__)
    subs = p.add_subparsers(dest="command", required=True)
    ins = subs.add_parser("install", help="Install/reinstall both agent integrations")
    ins.add_argument("--home", dest="install_home")
    ins.add_argument("--source-dir", default=str(Path(__file__).resolve().parents[2]), help=argparse.SUPPRESS)
    ins.add_argument("--bin-dir")
    ins.add_argument("--platform", choices=("all", "claude", "codex"), default="all")
    ins.add_argument("--non-interactive", action="store_true")
    ins.add_argument("--skip-validate", action="store_true", help="Skip live Pushover credential validation")
    ins.add_argument("--skip-test", action="store_true", help="Do not send an installation test notification")
    ins.add_argument("--env-only", action="store_true", help="Do not persist credentials")
    ins.add_argument("--uninstall", action="store_true")
    ins.add_argument("--purge", action="store_true")
    un = subs.add_parser("uninstall", help="Remove owned integration entries; retain unrelated settings")
    un.add_argument("--purge", action="store_true")
    hook = subs.add_parser("hook", help="Internal passive hook: one JSON object on stdin")
    hook.add_argument("--platform", choices=("claude", "codex"), required=True)
    notify = subs.add_parser("notify-codex", help="Adapter for Codex's completion-only legacy notify argument")
    notify.add_argument("payload")
    d = subs.add_parser("drain", help="Deliver queued alerts; --wait also waits for scheduled retries/reminders")
    d.add_argument("--wait", action="store_true")
    subs.add_parser("doctor", help="Check local installation; does not contact Pushover")
    test = subs.add_parser("test", help="Send a real test push")
    test.add_argument("--trigger", choices=("complete", "idle", "rate_limit", "error"), default="complete")
    replay = subs.add_parser("replay", help="Inspect a fixture or enqueue it")
    replay.add_argument("--platform", choices=("claude", "codex"), required=True)
    replay.add_argument("file", type=Path)
    replay.add_argument("--dry-run", action="store_true", help="Print sanitized notification JSON without writing state/sending")
    config = subs.add_parser("config", help="Inspect/change configuration without editing hook JSON")
    commands = config.add_subparsers(dest="config_command", required=True)
    commands.add_parser("show")
    change = commands.add_parser("set")
    change.add_argument("key", help="Dotted key such as triggers.error.enabled")
    change.add_argument("value", help="JSON value, or an unquoted string")
    run = subs.add_parser("run", help="Launch a CLI with optional terminal/JSON error observation")
    run.add_argument("platform", choices=("claude", "codex"))
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    return p


def doctor(home: Path) -> int:
    print(f"agent-pushover {__version__} · Python {sys.version.split()[0]}")
    print(f"Private directory: {home}")
    failures = 0
    config = load_config(home)
    try:
        load_credentials(home)
        print("Credentials: available (values hidden)")
    except (ValueError, OSError):
        print("Credentials: missing or invalid")
        failures += 1
    if (home / "disabled").exists():
        print("Integration is DISABLED/uninstalled")
        failures += 1
    manifest = read_object(home / "install-manifest.json")
    if not manifest:
        print("No installation manifest; run install.sh")
        failures += 1
    from .install import EVENTS, check_codex_settings
    for platform, info in manifest.get("platforms", {}).items():
        path = Path(info["path"])
        doc = read_object(path)
        found = 0
        for event, groups in doc.get("hooks", {}).items():
            for group in groups:
                found += sum(h.get("command") in info["commands"] for h in group.get("hooks", []))
        expected = len(EVENTS[platform])
        print(f"{platform} hooks: {found}/{expected} in {path}")
        failures += found != expected
        binary = "claude" if platform == "claude" else "codex"
        if shutil.which(binary):
            try:
                version = subprocess.run([binary, "--version"], stdin=subprocess.DEVNULL,
                                         capture_output=True, text=True, timeout=5)
                print(f"{binary} version: {version.stdout.strip()[:120] or 'not reported'}")
            except (OSError, subprocess.TimeoutExpired):
                print(f"{binary}: version check failed")
                failures += 1
        else:
            print(f"{binary}: not on PATH")
            failures += 1
        if platform == "codex":
            try:
                for warning in check_codex_settings(path):
                    print("Warning:", warning)
            except ValueError as exc:
                print(str(exc))
                failures += 1
            print("Codex trust: inspect /hooks in a new Codex process; doctor cannot attest trust or managed policy.")
        elif doc.get("disableAllHooks") is True:
            print("Claude Code disables user hooks in settings")
            failures += 1
    credential_file = home / "credentials.json"
    if credential_file.exists() and credential_file.stat().st_mode & 0o077:
        print("WARNING: credentials.json has group/other permissions; restrict it to mode 600")
        failures += 1
    with Store(home) as store:
        count = store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        print(f"Pending notifications/reminders: {count}")
    print("Triggers: " + ", ".join(f"{k}={'on' if v['enabled'] else 'off'} (priority {v['priority']})"
                                    for k, v in config["triggers"].items()))
    print(f"Diagnostic log (no message bodies): {home / 'events.log'}")
    print("No live push was sent. Run agent-pushover test to check Pushover acceptance.")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parser().parse_args(argv)
    home = home_path(args.home)
    # Keep parsing/notification errors out of agent decision channels, even on broken configuration.
    if args.command in ("hook", "notify-codex"):
        platform = args.platform if args.command == "hook" else "codex"
        try:
            raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1) if args.command == "hook" else args.payload
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("Hook payload too large")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
            handle(home, platform, data)
        except Exception as exc:
            log(home, "hook_failed", platform=platform, error_type=type(exc).__name__)
        if args.command == "hook" and platform == "codex":
            print("{}")  # Valid, decision-free JSON, including for Codex Stop.
        return 0
    try:
        if args.command == "install":
            from .install import install, uninstall
            if args.uninstall:
                return uninstall(home_path(args.install_home or args.home), args.purge)
            return install(args)
        if args.command == "uninstall":
            from .install import uninstall
            return uninstall(home, args.purge)
        if args.command == "drain":
            return drain(home, wait=args.wait)
        if args.command == "doctor":
            return doctor(home)
        if args.command == "config":
            config = load_config(home)
            if args.config_command == "show":
                print(json.dumps(config, indent=2))
            else:
                parts = args.key.split(".")
                with lock(home / "install.lock"):
                    config = load_config(home)
                    target = config
                    for key in parts[:-1]:
                        if key not in target or not isinstance(target[key], dict):
                            raise ValueError("Unknown config key")
                        target = target[key]
                    if parts[-1] not in target:
                        raise ValueError("Unknown config key")
                    try:
                        value = json.loads(args.value)
                    except ValueError:
                        value = args.value
                    target[parts[-1]] = value
                    validate_config(config)
                    write_json(home / "config.json", config)
                print(f"Updated {args.key}")
            return 0
        if args.command == "test":
            config = load_config(home)
            send(load_credentials(home), {"title": f"Agent Pushover · {args.trigger} test",
                                          "message": "Test notification from agent-pushover. No agent action was performed.",
                                          "priority": config["triggers"][args.trigger]["priority"]}, config)
            print("Pushover accepted the test message. Check the target device.")
            return 0
        if args.command == "replay":
            data = read_object(args.file, missing=False)
            if args.dry_run:
                event = classify(args.platform, data)
                config = load_config(home)
                try:
                    secrets = tuple(load_credentials(home).values())
                except ValueError:
                    secrets = ()
                output = render(event, config, secrets) if event else None
                print(json.dumps({"trigger": event.trigger if event else None, "notification": output}, indent=2))
            else:
                count = handle(home, args.platform, data)
                print(f"Queued {count} notification(s)/reminder(s).")
            return 0
        if args.command == "run":
            from .stream import run
            return run(home, args.platform, args.arguments)
    except (ValueError, OSError, KeyError, TypeError, DeliveryError) as exc:
        if isinstance(exc, DeliveryError):
            message = f"Pushover request failed: {exc.code}"
        elif isinstance(exc, json.JSONDecodeError):
            message = "Invalid JSON in a configuration or fixture file"
        else:
            message = str(exc)
        print(f"agent-pushover: {message}", file=sys.stderr)
        return 1
    return 0
