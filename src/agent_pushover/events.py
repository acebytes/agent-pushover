"""Pure event normalization. Never inspect prompts, source files, or transcripts."""
from __future__ import annotations

import hashlib
import json
import re
import socket
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .config import redact

# Deliberately stronger than simply matching the words 'rate limit'.
RATE = re.compile(
    r"(?i)(\brate_limit(?:_exceeded|_reached|_error)?\b|\busage_limit_reached\b|"
    r"\binsufficient_quota\b|\brate[ -]limit(?:ed|ing)?\b.{0,45}\b(?:reached|exceeded|hit)\b|"
    r"\b(?:hit|reached|exceeded)\b.{0,45}\b(?:usage |rate |spending )?limit\b|"
    r"\b(?:usage|quota)\b.{0,35}\b(?:exceeded|exhausted|reached)\b|"
    r"\btoo many requests\b|\b(?:HTTP(?: error)?|status(?: code)?|API error)[: ]+429\b)"
)
ERROR_LINE = re.compile(
    r"(?im)^\s*[•●✖×!│╰╭>\s]*(?:error\s*:|fatal\s*:|API\s+error\b|"
    r"error\s+(?:connecting|loading|reading|writing|sending|receiving|starting)\b|"
    r"traceback \(most recent call last\)|thread .{0,100} panicked|"
    r"stream disconnected\b|connection (?:refused|reset|timed out)\b|"
    r"authentication (?:failed|required)\b|unexpected (?:error|exception)\b)"
)
EXIT = re.compile(r"(?im)^\s*(?:exit code\s*:?\s*|process exited with code\s+)(-?\d+)\b")
IDLE_LINE = re.compile(
    r"(?i)(would you like to run the following command|do you want to proceed\?|"
    r"press (?:enter|return) to continue|waiting for (?:your|user) (?:input|approval)|"
    r"approval (?:is )?required)"
)
ATTENTION = {
    "permission_prompt": ("permission", "Approval needed", "A permission request is waiting for you."),
    "idle_prompt": ("idle", "Awaiting input", "The session is idle and waiting for your next input."),
    "agent_needs_input": ("question", "Input needed", "An agent is waiting for your input."),
    "elicitation_dialog": ("elicitation", "Input needed", "An MCP server is requesting your input."),
    "elicitation_url_dialog": ("elicitation", "Input needed", "An MCP server needs a browser action."),
}
LABELS = {"complete": "Turn complete", "idle": "Attention needed", "rate_limit": "Rate / usage limit",
          "error": "Error reported"}


def text_of(value: Any, budget: int = 12000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:budget]
    return json.dumps(value, ensure_ascii=False, default=str)[:budget]


def failed_response(value: Any, depth: int = 0) -> bool:
    """Detect explicit failure signals, not a successful file read discussing errors."""
    if depth > 6:
        return False
    if isinstance(value, dict):
        if value.get("isError") is True or value.get("is_error") is True or value.get("success") is False:
            return True
        if value.get("status") in ("failed", "error"):
            return True
        if value.get("error") not in (None, "", False, {}, []):
            return True
        for key in ("exit_code", "exitCode", "exit_status"):
            code = value.get(key)
            if type(code) in (int, str) and str(code).lstrip("-").isdigit() and int(code) != 0:
                return True
        # Do not recursively scan arbitrary stdout/content as source code could contain these words.
        return any(failed_response(value[k], depth + 1) for k in ("metadata", "result") if k in value)
    if isinstance(value, str):
        header = value[:2000]
        match = EXIT.search(header)
        return bool((match and int(match.group(1)) != 0) or ERROR_LINE.match(header))
    return False


@dataclass(frozen=True)
class Event:
    platform: str
    trigger: str
    session: str
    turn: str
    project: str
    source: str
    reason: str
    summary: str
    detail: str = ""
    agent: str = ""

    @property
    def session_key(self) -> str:
        return hashlib.sha256(f"{self.platform}\0{self.session}\0{self.agent}".encode()).hexdigest()

    def dedupe_key(self, generation: str = "") -> str:
        # Permissions and their delayed Notification counterpart share the same reason.
        if self.trigger == "rate_limit":
            discriminator = "rate_limit"
        elif self.trigger == "error":
            discriminator = self.reason + hashlib.sha256(self.detail.encode()).hexdigest()
        elif self.trigger == "complete":
            discriminator = self.turn or generation or self.detail
        else:
            discriminator = self.reason
        raw = f"{self.session_key}\0{self.trigger}\0{discriminator}"
        return hashlib.sha256(raw.encode()).hexdigest()


def classify(platform: str, data: dict, run_id: str = "") -> Event | None:
    if platform not in ("claude", "codex") or not isinstance(data, dict):
        return None
    event = str(data.get("hook_event_name", ""))
    if not event and data.get("type") == "agent-turn-complete":
        event = "Stop"
    session = run_id or str(data.get("session_id") or data.get("thread-id") or data.get("thread_id") or "unknown")
    turn = str(data.get("turn_id") or data.get("turn-id") or data.get("prompt_id") or "")
    cwd = str(data.get("cwd") or "")
    project = Path(cwd).name if cwd else "session"
    agent = str(data.get("agent_id") or "")
    tool = str(data.get("tool_name") or "tool")
    ti = data.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}

    def make(kind: str, reason: str, summary: str, detail: Any = "") -> Event:
        return Event(platform, kind, session, turn, project, event, reason, summary,
                     text_of(detail), agent)

    def failure(detail: Any, reason: str = "failure", structured: Any = "") -> Event:
        full = " ".join((text_of(structured), text_of(detail)))
        limited = bool(RATE.search(full))
        return make("rate_limit" if limited else "error", "rate_limit" if limited else reason,
                    "A rate or usage limit was reported; check the session before resuming."
                    if limited else "An error was reported. The agent may retry or need your help.", full.strip())

    if event == "Stop":
        if agent:  # Never report an internal subagent stop as the main task finishing.
            return None
        last = data.get("last_assistant_message", data.get("last-assistant-message", ""))
        # This is conversational text, not a reliable success/failure classifier.
        return make("complete", "stop", "The agent's turn has stopped. Review the result in your terminal.", last)
    if event == "StopFailure":
        return failure(data.get("error_details") or data.get("last_assistant_message"), "api_failure", data.get("error"))
    if event == "PostToolUseFailure":
        if data.get("is_interrupt") is True:
            return None
        return failure(data.get("error"), f"tool:{tool}")
    if event == "PostToolUse":
        response = data.get("tool_response")
        if failed_response(response):
            return failure(response, f"tool:{tool}")
        return None
    if event == "PermissionDenied":
        return make("error", "permission_denied", "A tool action was denied by the permission policy.", data.get("reason"))
    if event == "PermissionRequest":
        detail = ti.get("description") or ti.get("command") or tool
        return make("idle", "permission", "A permission request is waiting for you.", detail)
    if event == "PreToolUse":
        if tool in ("AskUserQuestion", "request_user_input"):
            questions = ti.get("questions")
            details = []
            if isinstance(questions, list):
                details = [q.get("question", "") for q in questions[:5] if isinstance(q, dict)]
            return make("idle", "question", "The agent is asking for a human decision.", "\n".join(map(str, details)))
        if tool == "ExitPlanMode":
            return make("idle", "plan", "The agent is submitting a plan for approval.")
    if event == "Elicitation":
        return make("idle", "elicitation", "An MCP server is requesting your input.", data.get("message"))
    if event == "Notification":
        nt = str(data.get("notification_type") or "")
        if nt in ATTENTION:
            reason, _, summary = ATTENTION[nt]
            return make("idle", reason, summary, data.get("message"))
        # Unknown notifications are not assumed to be errors. Recognize explicit failure text only.
        message = text_of(data.get("message"))
        if RATE.search(message) or ERROR_LINE.search(message):
            return failure(message, "notification_failure")
    if event == "RuntimeError":
        return failure(data.get("message") or data.get("error"), "runtime", data.get("code"))
    if event == "RuntimeIdle":
        return make("idle", "permission" if data.get("permission") else "question",
                    "A terminal prompt appears to need your attention (text-detected).", data.get("message"))
    if event == "IdleReminder":
        return make("idle", "idle", "No new input was observed after the turn stopped. Check the session.")
    return None


def render(event: Event, config: dict, secrets: tuple[str, ...] = ()) -> dict:
    platform = "Claude Code" if event.platform == "claude" else "Codex"
    label = LABELS[event.trigger]
    if event.trigger == "idle":
        label = {"permission": "Approval needed", "question": "Decision needed", "plan": "Plan approval",
                 "idle": "Awaiting input", "elicitation": "Input needed"}.get(event.reason, label)
    project = event.project if config["privacy"]["include_project"] else "session"
    title = f"{platform} · {label} · {project}"
    body = event.summary
    if config["privacy"]["include_details"] and event.detail:
        body += "\n\n" + event.detail
    footer = []
    if config["privacy"]["include_host"]:
        footer.append(socket.gethostname().split(".")[0])
    footer.append("session " + event.session[:8])
    if event.agent:
        footer.append("subagent " + event.agent[:8])
    footer.append(event.source)
    body += "\n\n" + " · ".join(footer)
    return {"title": redact(title, secrets)[:250], "message": redact(body, secrets)[:1024],
            "priority": config["triggers"][event.trigger]["priority"]}
