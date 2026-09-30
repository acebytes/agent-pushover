import copy
import unittest

from helpers import ROOT
from agent_pushover.config import DEFAULTS, redact, validate_config
from agent_pushover.events import RATE, classify, failed_response, render


class EventTests(unittest.TestCase):
    def event(self, name, platform="claude", **data):
        return classify(platform, {"hook_event_name": name, "session_id": "abc12345", "cwd": "/repo/demo", **data})

    def test_stop_both_platforms(self):
        for p in ("claude", "codex"):
            self.assertEqual(self.event("Stop", p).trigger, "complete")

    def test_stop_is_not_semantic_success_detector(self):
        self.assertEqual(self.event("Stop", last_assistant_message="I implemented rate limit handling.").trigger, "complete")

    def test_subagent_stop_not_main_completion(self):
        self.assertIsNone(self.event("Stop", agent_id="agent-1"))
        self.assertIsNone(self.event("SubagentStop", agent_id="agent-1"))

    def test_rate_limit_structured(self):
        self.assertEqual(self.event("StopFailure", error="rate_limit").trigger, "rate_limit")

    def test_usage_limits(self):
        for message in ("You've hit your usage limit. Try later.", "429 Too Many Requests",
                        "usage_limit_reached", "insufficient_quota", "Rate limit exceeded",
                        "API error: 429", "Quota exhausted"):
            with self.subTest(message=message):
                self.assertEqual(self.event("RuntimeError", message=message).trigger, "rate_limit")

    def test_overloaded_not_rate_limit(self):
        self.assertEqual(self.event("StopFailure", error="overloaded").trigger, "error")

    def test_auth_and_unknown_api_errors(self):
        for error in ("authentication_failed", "server_error", "unknown", "billing_error"):
            self.assertEqual(self.event("StopFailure", error=error).trigger, "error")

    def test_failure_hook(self):
        self.assertEqual(self.event("PostToolUseFailure", error="Exit code 1\nTests failed").trigger, "error")

    def test_interrupt_ignored(self):
        self.assertIsNone(self.event("PostToolUseFailure", error="Aborted", is_interrupt=True))

    def test_mcp_rate_limit(self):
        e = self.event("PostToolUse", "codex", tool_response={"isError": True, "content": [{"text": "Too many requests"}]})
        self.assertEqual(e.trigger, "rate_limit")

    def test_exit_codes_detected(self):
        for response in ({"exit_code": 2}, {"metadata": {"exit_code": 1}},
                         "Process exited with code 1\nOutput:\nFAIL", "Exit code 127\nNot found",
                         {"is_error": True}, {"status": "failed"}):
            with self.subTest(response=response):
                self.assertEqual(self.event("PostToolUse", "codex", tool_response=response).trigger, "error")

    def test_successful_read_is_not_scanned(self):
        for response in ({"stdout": "Error: Rate limit reached", "exit_code": 0},
                         {"content": [{"text": "Rate limit exceeded"}]}, "Here is how to handle Error: xyz"):
            self.assertIsNone(self.event("PostToolUse", "codex", tool_response=response))

    def test_permission_payload_does_not_become_error(self):
        e = self.event("PermissionRequest", tool_input={"command": "echo 'Error: rate limit reached'"})
        self.assertEqual(e.trigger, "idle")

    def test_question_tools(self):
        for tool in ("AskUserQuestion", "request_user_input"):
            e = self.event("PreToolUse", tool_name=tool, tool_input={"questions": [{"question": "Which database?"}]})
            self.assertEqual(e.reason, "question")
            self.assertEqual(e.detail, "Which database?")

    def test_plan_and_elicitation(self):
        self.assertEqual(self.event("PreToolUse", tool_name="ExitPlanMode").reason, "plan")
        self.assertEqual(self.event("Elicitation").reason, "elicitation")

    def test_notification_types(self):
        for nt in ("permission_prompt", "idle_prompt", "agent_needs_input", "elicitation_dialog", "elicitation_url_dialog"):
            self.assertEqual(self.event("Notification", notification_type=nt).trigger, "idle")
        self.assertIsNone(self.event("Notification", notification_type="auth_success", message="Authenticated"))

    def test_unknown_notification_explicit_error(self):
        self.assertEqual(self.event("Notification", message="Error: connection refused").trigger, "error")

    def test_permission_denied(self):
        self.assertEqual(self.event("PermissionDenied", reason="Policy").trigger, "error")

    def test_legacy_notify_argument(self):
        e = classify("codex", {"type": "agent-turn-complete", "thread-id": "t1", "turn-id": "turn1",
                               "last-assistant-message": "Done"})
        self.assertEqual((e.trigger, e.session, e.turn, e.detail), ("complete", "t1", "turn1", "Done"))

    def test_ignore_unsupported_event(self):
        self.assertIsNone(self.event("MadeUpFailure"))

    def test_default_privacy(self):
        e = self.event("Stop", last_assistant_message="PRIVATE_SOURCE_CODE")
        m = render(e, DEFAULTS)
        self.assertNotIn("PRIVATE_SOURCE_CODE", str(m))
        self.assertIn("demo", m["title"])

    def test_details_and_pushover_size_limits(self):
        config = copy.deepcopy(DEFAULTS)
        config["privacy"]["include_details"] = True
        e = self.event("Stop", last_assistant_message="🔥" * 5000)
        m = render(e, config)
        self.assertLessEqual(len(m["message"]), 1024)
        self.assertLessEqual(len(m["title"]), 250)
        self.assertIn("🔥", m["message"])

    def test_priorities(self):
        for name, kwargs, expected in (("Stop", {}, 0), ("PermissionRequest", {}, 0),
                                       ("StopFailure", {"error": "rate_limit"}, 1),
                                       ("StopFailure", {"error": "server_error"}, 1)):
            self.assertEqual(render(self.event(name, **kwargs), DEFAULTS)["priority"], expected)

    def test_redaction(self):
        text = "Bearer abcXYZ password=secret token: \"private\" sk-abcdefghijklmnop TOKENEXACT"
        result = redact(text, ("TOKENEXACT",))
        for secret in ("abcXYZ", "secret", "private", "sk-abcdefgh", "TOKENEXACT"):
            self.assertNotIn(secret, result)

    def test_no_emergency_priority(self):
        config = copy.deepcopy(DEFAULTS)
        config["triggers"]["error"]["priority"] = 2
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_invalid_config_types(self):
        config = copy.deepcopy(DEFAULTS)
        config["triggers"]["error"]["enabled"] = "false"
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_cross_session_dedupe_isolation(self):
        a = self.event("StopFailure", error="rate_limit")
        b = classify("claude", {"hook_event_name": "StopFailure", "session_id": "different", "error": "rate_limit"})
        self.assertNotEqual(a.dedupe_key(), b.dedupe_key())

    def test_permission_duplicate_normalization(self):
        a = self.event("PermissionRequest")
        b = self.event("Notification", notification_type="permission_prompt")
        self.assertEqual(a.dedupe_key(), b.dedupe_key())


if __name__ == "__main__":
    unittest.main()
