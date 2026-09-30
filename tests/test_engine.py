import concurrent.futures
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from helpers import configured_home, jobs
from agent_pushover.cli import main
from agent_pushover.config import DEFAULTS, load_config, write_json
from agent_pushover.engine import Store, drain, handle, safe_handle
from agent_pushover.pushover import DeliveryError


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = configured_home(self.temp.name)
        self.sent = []

    def tearDown(self):
        self.temp.cleanup()

    def handle(self, name, platform="claude", now=100, **kw):
        data = {"hook_event_name": name, "session_id": "session-1", "cwd": "/repo/demo", **kw}
        return handle(self.home, platform, data, spawn=False, now=now, environ={})

    def send(self, credentials, message, config):
        self.sent.append(message)

    def drain(self, now=100, **kwargs):
        return drain(self.home, sender=self.send, clock=lambda: now, **kwargs)

    def test_queued_then_delivered(self):
        self.assertEqual(self.handle("Stop"), 1)
        self.assertEqual(len(jobs(self.home)), 1)
        self.drain()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(jobs(self.home), [])

    def test_duplicate_pending_permission(self):
        self.handle("PermissionRequest")
        self.assertEqual(self.handle("Notification", notification_type="permission_prompt"), 0)
        self.assertEqual(len(jobs(self.home)), 1)

    def test_recently_sent_dedup(self):
        self.handle("StopFailure", error="rate_limit")
        self.drain()
        self.assertEqual(self.handle("StopFailure", now=110, error="rate_limit"), 0)
        self.assertEqual(self.handle("StopFailure", now=221, error="rate_limit"), 1)

    def test_independent_error_details(self):
        self.handle("StopFailure", error="unknown", error_details="first failure")
        self.handle("StopFailure", error="unknown", error_details="second failure")
        self.assertEqual(len(jobs(self.home)), 2)

    def test_complete_disabled_idle_still_scheduled(self):
        config = load_config(self.home)
        config["triggers"]["complete"]["enabled"] = False
        write_json(self.home / "config.json", config)
        self.handle("Stop", "codex")
        self.assertEqual([j["trigger"] for j in jobs(self.home)], ["idle"])
        self.drain(now=160)
        self.assertEqual(len(self.sent), 1)

    def test_codex_idle_waits(self):
        self.handle("Stop", "codex")
        self.drain(now=100)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual([j["trigger"] for j in jobs(self.home)], ["idle"])
        self.drain(now=160)
        self.assertEqual(len(self.sent), 2)

    def test_codex_new_prompt_cancels_idle(self):
        self.handle("Stop", "codex")
        self.handle("UserPromptSubmit", "codex", now=130)
        self.drain(now=161)
        self.assertEqual(len(self.sent), 1)

    def test_codex_tool_continuation_cancels_idle(self):
        self.handle("Stop", "codex")
        self.handle("PreToolUse", "codex", now=101, tool_name="Bash")
        self.drain(now=161)
        self.assertEqual(len(self.sent), 1)

    def test_wrapper_input_cancels_idle(self):
        self.handle("Stop", "codex")
        self.handle("Input", "codex", now=130)
        self.drain(now=161)
        self.assertEqual(len(self.sent), 1)

    def test_exit_cancels_idle(self):
        self.handle("Stop", "codex")
        self.handle("SessionEnd", "codex", now=101)
        self.drain(now=161)
        self.assertEqual(len(self.sent), 1)

    def test_error_cancels_idle(self):
        self.handle("Stop", "codex")
        self.handle("RuntimeError", "codex", now=101, message="Error: API unavailable")
        self.drain(now=161)
        self.assertEqual([j["trigger"] for j in jobs(self.home)], [])
        self.assertEqual(len(self.sent), 2)

    def test_json_mode_uses_stream_completion_only(self):
        env = {"AGENT_PUSHOVER_STREAM": "json", "AGENT_PUSHOVER_RUN_ID": "run1"}
        data = {"hook_event_name": "Stop", "session_id": "s1"}
        handle(self.home, "codex", data, spawn=False, now=100, environ=env)
        self.assertEqual(jobs(self.home), [])
        handle(self.home, "codex", {**data, "_stream_event": True}, spawn=False, now=100, environ=env)
        self.assertEqual([j["trigger"] for j in jobs(self.home)], ["complete"])

    def test_back_to_back_turns_not_deduped(self):
        self.handle("UserPromptSubmit")
        self.handle("Stop", last_assistant_message="Done")
        self.drain()
        self.handle("UserPromptSubmit", now=101)
        self.assertEqual(self.handle("Stop", now=101, last_assistant_message="Done"), 1)

    def test_disabled_trigger_not_enqueued(self):
        config = load_config(self.home)
        config["triggers"]["error"]["enabled"] = False
        write_json(self.home / "config.json", config)
        self.assertEqual(self.handle("StopFailure", error="unknown"), 0)

    def test_toggle_cancels_already_pending_job(self):
        self.handle("StopFailure", error="unknown")
        config = load_config(self.home)
        config["triggers"]["error"]["enabled"] = False
        write_json(self.home / "config.json", config)
        self.drain()
        self.assertEqual(self.sent, [])

    def test_retry_then_success(self):
        now = [100]
        attempts = []
        def sender(*args):
            attempts.append(1)
            if len(attempts) < 3:
                raise DeliveryError("network_or_tls_error", True)
            self.sent.append(args[1])
        self.handle("Stop")
        drain(self.home, wait=True, sender=sender, clock=lambda: now[0], sleep=lambda n: now.__setitem__(0, now[0] + n))
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(jobs(self.home), [])

    def test_permanent_error_not_retried(self):
        self.handle("Stop")
        def sender(*args):
            raise DeliveryError("pushover_http_429", False)
        drain(self.home, sender=sender, clock=lambda: 100)
        self.assertEqual(jobs(self.home), [])
        self.assertIn("pushover_http_429", (self.home / "events.log").read_text())

    def test_retry_budget_exhausted(self):
        now = [100]
        attempts = []
        def sender(*args):
            attempts.append(1)
            raise DeliveryError("network_or_tls_error", True)
        self.handle("Stop")
        drain(self.home, wait=True, sender=sender, clock=lambda: now[0], sleep=lambda n: now.__setitem__(0, now[0] + n))
        self.assertEqual(len(attempts), 3)
        self.assertEqual(jobs(self.home), [])

    def test_queue_survives_reopen(self):
        self.handle("Stop")
        with Store(self.home) as store:
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        self.drain()
        self.assertEqual(len(self.sent), 1)

    def test_stale_messages_expire(self):
        self.handle("Stop")
        self.drain(now=4001)
        self.assertEqual(self.sent, [])

    def test_uninstalled_is_noop(self):
        (self.home / "disabled").touch()
        self.assertEqual(self.handle("Stop"), 0)

    def test_no_raw_detail_in_queue_or_log(self):
        self.handle("Stop", last_assistant_message="SECRET_PROMPT_DETAILS")
        self.drain()
        self.assertNotIn("SECRET_PROMPT_DETAILS", (self.home / "events.log").read_text())
        self.assertNotIn("SECRET_PROMPT_DETAILS", str(self.sent))

    def test_config_failure_is_passive(self):
        (self.home / "config.json").write_text("INVALID")
        self.assertEqual(safe_handle(self.home, "claude", {"hook_event_name": "Stop"}, spawn=False), 0)

    def test_hook_invalid_input_no_decision(self):
        for platform, expected in (("claude", ""), ("codex", "{}\n")):
            output = io.StringIO()
            stream = io.TextIOWrapper(io.BytesIO(b"not-json"))
            with patch("sys.stdin", stream), redirect_stdout(output):
                code = main(["--home", str(self.home), "hook", "--platform", platform])
            self.assertEqual(code, 0)
            self.assertEqual(output.getvalue(), expected)

    def test_parallel_identical_events(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: self.handle("StopFailure", error="rate_limit"), range(24)))
        self.assertEqual(len(jobs(self.home)), 1)

    def test_private_files(self):
        self.handle("Stop")
        self.assertEqual(self.home.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.home / "state.sqlite3").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
