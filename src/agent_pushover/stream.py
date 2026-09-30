"""Optional POSIX CLI wrapper: byte-preserving pipes/PTY plus bounded observers.

A terminal observer is heuristic, not an error API. It never reads source files,
saves a terminal transcript, injects answers, or turns silent computation into idle.
"""
from __future__ import annotations

import codecs
import errno
import fcntl
import json
import os
import pty
import re
import selectors
import shutil
import signal
import subprocess
import sys
import termios
import time
import tty
import uuid
from pathlib import Path
from typing import Callable

from .config import load_config
from .engine import recent_failure, safe_handle
from .events import ERROR_LINE, IDLE_LINE, RATE, failed_response, text_of


class AnsiStripper:
    """Incremental CSI/OSC removal, including escape sequences split across reads."""
    def __init__(self):
        self.state = "text"

    def feed(self, text: str) -> str:
        out = []
        for ch in text:
            if self.state == "text":
                if ch == "\x1b":
                    self.state = "esc"
                elif ch in "\n\r":
                    out.append("\n")
                elif ord(ch) >= 32 or ch == "\t":
                    out.append(ch)
            elif self.state == "esc":
                if ch == "[":
                    self.state = "csi"
                elif ch in "]P^_":
                    self.state = "osc"
                else:
                    self.state = "text"
            elif self.state == "csi":
                if "@" <= ch <= "~":
                    if ch in "HfJK":
                        out.append("\n")
                    self.state = "text"
            elif self.state == "osc":
                if ch == "\x07":
                    self.state = "text"
                elif ch == "\x1b":
                    self.state = "osc_esc"
            elif self.state == "osc_esc":
                self.state = "text" if ch == "\\" else "osc"
        return "".join(out)


class TerminalObserver:
    def __init__(self, emit: Callable[[dict], None], config: dict):
        self.emit = emit
        self.enabled = config["terminal"]["enabled"]
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.ansi = AnsiStripper()
        self.buffer = ""
        self.seen: dict[tuple[str, str], float] = {}
        self.fragment_emitted = False
        self.extra = {kind: [re.compile(p, re.I | re.M) for p in config["terminal"][key]]
                      for kind, key in (("rate", "extra_rate_limit_patterns"),
                                        ("error", "extra_error_patterns"), ("idle", "extra_idle_patterns"))}

    def check(self, line: str) -> None:
        if self.fragment_emitted or not self.enabled or not line.strip():
            return
        def matches(kind, builtin):
            return builtin.search(line) or any(p.search(line) for p in self.extra[kind])
        rate = matches("rate", RATE)
        error = matches("error", ERROR_LINE)
        idle = matches("idle", IDLE_LINE)
        if not (rate or error or idle):
            return
        kind = "RuntimeError" if rate or error else "RuntimeIdle"
        # Normalize short redraw variations; queue-level dedupe handles repeated full frames.
        signature = (kind, line.strip()[:240])
        now = time.monotonic()
        # Suppress a redraw burst, not every identical error for the entire session.
        # The durable queue owns the longer per-trigger cooldown.
        previous = self.seen.get(signature)
        if previous is not None and now - previous < 2:
            return
        self.seen = {key: at for key, at in self.seen.items() if now - at < 2}
        if len(self.seen) >= 256:
            self.seen.pop(next(iter(self.seen)))
        self.seen[signature] = now
        self.fragment_emitted = True
        self.emit({"hook_event_name": kind, "message": line[:2000],
                   "code": "rate_limit" if rate else "",
                   "permission": bool(idle and re.search(r"(?i)approval|proceed|following command", line))})

    def feed(self, data: bytes, final: bool = False) -> None:
        text = self.ansi.feed(self.decoder.decode(data, final=final))
        for part in text.splitlines(keepends=True):
            self.buffer = (self.buffer + part.rstrip("\n"))[-4096:]
            if part.endswith("\n"):
                self.check(self.buffer)
                self.buffer = ""
                self.fragment_emitted = False
        if final:
            self.flush()

    def flush(self) -> None:
        """Check unterminated output once it has been quiet for one polling interval."""
        self.check(self.buffer)


class JsonObserver:
    """Codex exec --json: inspect event envelopes, not text in successful output."""
    MAX_LINE = 2 * 1024 * 1024

    def __init__(self, emit: Callable[[dict], None]):
        self.emit = emit
        self.buffer = bytearray()
        self.overflow = False
        self.last_message = ""
        self.turn = 0
        self.completed = False

    def event(self, data: dict) -> None:
        kind = data.get("type")
        common = {"turn_id": str(data.get("turn_id") or f"json-turn-{self.turn}"), "_stream_event": True}
        if kind == "thread.started":
            self.emit({**common, "hook_event_name": "SessionStart"})
        elif kind == "turn.started":
            self.turn += 1
            self.last_message = ""
            self.emit({"hook_event_name": "UserPromptSubmit", "turn_id": f"json-turn-{self.turn}"})
        elif kind in ("error", "turn.failed"):
            error = data.get("error") or data.get("message") or "Codex reported a failed turn."
            self.emit({**common, "hook_event_name": "RuntimeError", "message": text_of(error),
                       "code": data.get("code", "")})
        elif kind == "item.completed":
            item = data.get("item")
            if not isinstance(item, dict):
                return
            if item.get("type") == "agent_message":
                self.last_message = text_of(item.get("text"), 4000)
            elif item.get("type") == "command_execution" and failed_response(item):
                self.emit({**common, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                           "tool_response": item})
            elif item.get("type") in ("mcp_tool_call", "file_change", "error") and (
                    failed_response(item) or item.get("type") == "error"):
                self.emit({**common, "hook_event_name": "RuntimeError", "message": text_of(item)})
        elif kind == "turn.completed":
            self.completed = True
            self.emit({**common, "hook_event_name": "Stop", "last_assistant_message": self.last_message})

    def feed(self, data: bytes, final: bool = False) -> None:
        # Bound memory even for a malformed/no-newline stream. Never parse a truncated line.
        for part in data.splitlines(keepends=True):
            if not self.overflow:
                self.buffer.extend(part)
                if len(self.buffer) > self.MAX_LINE:
                    self.buffer.clear()
                    self.overflow = True
            if part.endswith(b"\n"):
                if not self.overflow:
                    self.parse(bytes(self.buffer))
                self.buffer.clear()
                self.overflow = False
        if final and self.buffer and not self.overflow:
            self.parse(bytes(self.buffer))
            self.buffer.clear()

    def flush(self) -> None:
        pass  # JSON records must be complete; do not guess from fragments.

    def parse(self, line: bytes) -> None:
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                self.event(data)
        except (ValueError, UnicodeError):
            pass


def _write(fd: int, data: bytes) -> None:
    while data:
        count = os.write(fd, data)
        data = data[count:]


def _signals(pid: int, resize=None) -> dict:
    previous = {}
    def forward(sig, _frame):
        if sig == signal.SIGWINCH and resize is not None:
            resize()
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGWINCH):
        previous[sig] = signal.signal(sig, forward)
    return previous


def _restore(previous: dict) -> None:
    for sig, handler in previous.items():
        signal.signal(sig, handler)


def run_pipes(command: list[str], env: dict, stdout_observer, stderr_observer) -> int:
    proc = subprocess.Popen(command, env=env, stdin=None, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    previous = _signals(proc.pid)
    try:
        with selectors.DefaultSelector() as selector:
            for stream, dest, observer in ((proc.stdout, 1, stdout_observer), (proc.stderr, 2, stderr_observer)):
                selector.register(stream, selectors.EVENT_READ, (dest, observer))
            while selector.get_map():
                ready = selector.select(timeout=0.5)
                if not ready:
                    stdout_observer.flush()
                    stderr_observer.flush()
                for key, _ in ready:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    dest, observer = key.data
                    if chunk:
                        _write(dest, chunk)
                        observer.feed(chunk)
                    else:
                        observer.feed(b"", final=True)
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
        rc = proc.wait()
        return rc if rc >= 0 else 128 - rc
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        raise
    finally:
        _restore(previous)
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()


def run_pty(command: list[str], env: dict, observer, input_seen: Callable[[], None]) -> int:
    old_terminal = termios.tcgetattr(0)
    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvpe(command[0], command, env)
        except OSError:
            os.write(2, b"Error: failed to launch coding agent\n")
            os._exit(127)
    def resize():
        try:
            size = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(master, termios.TIOCSWINSZ, size)
        except OSError:
            pass
    resize()
    previous = _signals(pid, resize)
    try:
        tty.setraw(0)
        with selectors.DefaultSelector() as selector:
            selector.register(master, selectors.EVENT_READ)
            selector.register(0, selectors.EVENT_READ)
            done = False
            while not done:
                ready = selector.select(timeout=0.5)
                if not ready:
                    observer.flush()
                for key, _ in ready:
                    if key.fd == master:
                        try:
                            chunk = os.read(master, 65536)
                        except OSError as exc:
                            if exc.errno == errno.EIO:
                                chunk = b""
                            else:
                                raise
                        if not chunk:
                            done = True
                            break
                        _write(1, chunk)
                        observer.feed(chunk)
                    else:
                        chunk = os.read(0, 4096)
                        if chunk:
                            # Observe that input occurred, but never inspect or persist its bytes.
                            input_seen()
                            _write(master, chunk)
                        else:
                            selector.unregister(0)
                            _write(master, b"\x04")
        observer.feed(b"", final=True)
        _, status = os.waitpid(pid, 0)
        code = os.waitstatus_to_exitcode(status)
        return code if code >= 0 else 128 - code
    except BaseException:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        raise
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, old_terminal)
        _restore(previous)
        os.close(master)


def run(home: Path, platform: str, arguments: list[str]) -> int:
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    binary = "claude" if platform == "claude" else "codex"
    executable = shutil.which(binary)
    if not executable:
        print(f"{binary} is not on PATH; install the official CLI first.", file=sys.stderr)
        return 127
    run_id = uuid.uuid4().hex
    json_mode = platform == "codex" and ("exec" in arguments or "e" in arguments) and "--json" in arguments
    env = {**os.environ, "AGENT_PUSHOVER_RUN_ID": run_id, "AGENT_PUSHOVER_HOME": str(home),
           "AGENT_PUSHOVER_STREAM": "json" if json_mode else "terminal"}
    def emit(data: dict):
        safe_handle(home, platform, {"session_id": run_id, "cwd": os.getcwd(), **data}, environ=env)
    config = load_config(home)
    terminal = TerminalObserver(emit, config)
    stdout = JsonObserver(emit) if json_mode else terminal
    stderr = TerminalObserver(emit, config)
    emit({"hook_event_name": "SessionStart"})
    try:
        command = [executable, *arguments]
        # Exec is deliberately pipe-based so --json stdout remains machine-readable.
        interactive = sys.stdin.isatty() and sys.stdout.isatty() and not json_mode \
            and not any(a in arguments for a in ("exec", "e", "--help", "--version"))
        if interactive:
            rc = run_pty(command, env, terminal, lambda: emit({"hook_event_name": "Input"}))
        else:
            rc = run_pipes(command, env, stdout, stderr)
        if rc not in (0, 130, 143) and not recent_failure(home, platform, run_id):
            emit({"hook_event_name": "RuntimeError", "message": f"{binary} exited with status {rc}."})
        return rc
    except BrokenPipeError:
        return 141
    except OSError as exc:
        emit({"hook_event_name": "RuntimeError", "message": f"Unable to launch or observe {binary}."})
        print(f"Unable to launch or observe {binary}: {type(exc).__name__}", file=sys.stderr)
        return 127
    finally:
        emit({"hook_event_name": "SessionEnd"})
