import contextlib
import copy
import fcntl
import json
import os
import selectors
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest import mock
from pathlib import Path

from helpers import ROOT
from agent_pushover.config import DEFAULTS
from agent_pushover.events import classify
from agent_pushover.stream import AnsiStripper, JsonObserver, TerminalObserver, run_pipes


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.terminal = TerminalObserver(self.events.append, DEFAULTS)

    def test_rate_limit_with_ansi_chunk_boundaries(self):
        for chunk in (b'\x1b[3', b"1mYou've hit your usa", b'ge limit.\x1b[0m\n'):
            self.terminal.feed(chunk)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(classify("codex", self.events[0]).trigger, "rate_limit")
        self.assertNotIn("\x1b", self.events[0]["message"])

    def test_error_and_idle(self):
        self.terminal.feed(b'Error: upstream disconnected\n')
        self.terminal.feed(b'Would you like to run the following command?')
        self.terminal.flush()
        self.assertEqual([x["hook_event_name"] for x in self.events], ["RuntimeError", "RuntimeIdle"])

    def test_partial_errors_are_coalesced(self):
        self.terminal.feed(b'Error: ')
        self.terminal.feed(b'failure details\n')
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["message"], 'Error: failure details')

    def test_no_newline_error_flush(self):
        self.terminal.feed(b'Error: network unavailable')
        self.assertEqual(self.events, [])
        self.terminal.flush()
        self.terminal.flush()
        self.assertEqual(len(self.events), 1)

    def test_repeated_frame_dedup(self):
        self.terminal.feed(b'\r\x1b[2KAPI Error: 429 Too Many Requests\r')
        self.terminal.feed(b'\r\x1b[2KAPI Error: 429 Too Many Requests\r')
        self.assertEqual(len(self.events), 1)

    def test_same_error_can_be_reported_again_after_redraw_window(self):
        with mock.patch("agent_pushover.stream.time.monotonic", side_effect=[100, 101, 103]):
            for _ in range(3):
                self.terminal.feed(b'Error: unavailable\n')
        self.assertEqual(len(self.events), 2)

    def test_silence_is_not_idle(self):
        self.terminal.feed(b'Working...')
        for _ in range(20):
            self.terminal.feed(b'')
            self.terminal.flush()
        self.assertEqual(self.events, [])

    def test_success_description_not_error(self):
        self.terminal.feed(b'Implemented error handling and a rate limit test.\n')
        self.assertEqual(self.events, [])

    def test_unicode_split(self):
        data = '• Error: café unavailable\n'.encode()
        for byte in data:
            self.terminal.feed(bytes([byte]))
        self.assertEqual(len(self.events), 1)
        self.assertIn("café", self.events[0]["message"])

    def test_osc_payloads_are_not_scanned(self):
        for chunk in (b'\x1b]0;Error: should not notify', b'\x1b', b'\\Ready\n'):
            self.terminal.feed(chunk)
        self.assertEqual(self.events, [])

    def test_terminal_config_disabled(self):
        config = copy.deepcopy(DEFAULTS)
        config["terminal"]["enabled"] = False
        t = TerminalObserver(self.events.append, config)
        t.feed(b'Error: example\n')
        self.assertEqual(self.events, [])

    def test_custom_patterns(self):
        config = copy.deepcopy(DEFAULTS)
        config["terminal"]["extra_rate_limit_patterns"] = [r"^LIMIT REACHED!"]
        t = TerminalObserver(self.events.append, config)
        t.feed(b'LIMIT REACHED!\n')
        self.assertEqual(classify("codex", self.events[0]).trigger, "rate_limit")

    def test_buffer_is_bounded(self):
        self.terminal.feed(b'x' * 100000)
        self.assertLessEqual(len(self.terminal.buffer), 4096)

    def test_json_stream_complete_and_failure(self):
        j = JsonObserver(self.events.append)
        messages = [{"type": "thread.started", "thread_id": "s1"}, {"type": "turn.started"},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "Done"}},
                    {"type": "turn.completed"},
                    {"type": "error", "message": "Usage limit reached"},
                    {"type": "turn.failed", "error": {"message": "API unavailable"}}]
        raw = b''.join(json.dumps(m).encode() + b'\n' for m in messages)
        for n in range(0, len(raw), 7):
            j.feed(raw[n:n+7])
        self.assertEqual([x["hook_event_name"] for x in self.events],
                         ["SessionStart", "UserPromptSubmit", "Stop", "RuntimeError", "RuntimeError"])
        self.assertEqual(self.events[2]["last_assistant_message"], "Done")
        self.assertTrue(self.events[2]["_stream_event"])
        self.assertEqual(classify("codex", self.events[3]).trigger, "rate_limit")

    def test_json_success_text_not_error(self):
        j = JsonObserver(self.events.append)
        j.event({"type": "item.completed", "item": {"type": "agent_message", "text": "Error: rate limit reached"}})
        self.assertEqual(self.events, [])

    def test_json_tool_failure(self):
        j = JsonObserver(self.events.append)
        j.event({"type": "item.completed", "item": {"type": "command_execution", "exit_code": 1,
                                                       "aggregated_output": "failed tests"}})
        self.assertEqual(classify("codex", self.events[0]).trigger, "error")

    def test_json_unknown_and_malformed(self):
        j = JsonObserver(self.events.append)
        j.feed(b'not-json\n[]\n{"type":"future_event"}\n')
        self.assertEqual(self.events, [])

    def test_json_truncated_record_not_classified_until_eof(self):
        j = JsonObserver(self.events.append)
        j.feed(b'{"type":"error","message":"API unavailable"}')
        j.flush()
        self.assertEqual(self.events, [])
        j.feed(b'', final=True)
        self.assertEqual(len(self.events), 1)

    def test_json_memory_bound_and_recovers(self):
        j = JsonObserver(self.events.append)
        j.feed(b'x' * (j.MAX_LINE + 10))
        self.assertLessEqual(len(j.buffer), j.MAX_LINE)
        j.feed(b'\n{"type":"error","message":"problem"}\n')
        self.assertEqual(len(self.events), 1)


class ProcessTests(unittest.TestCase):
    def test_pipes_preserve_stdout_stderr_and_exit_status(self):
        events = []
        stdout_observer = JsonObserver(events.append)
        stderr_observer = TerminalObserver(events.append, DEFAULTS)
        out_data = b'{"type":"turn.failed","error":{"message":"rate_limit"}}\n'
        err_data = b'ordinary diagnostic\n'
        code = f'import os; os.write(1,{out_data!r}); os.write(2,{err_data!r}); raise SystemExit(7)'
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            old_out, old_err = os.dup(1), os.dup(2)
            try:
                os.dup2(out.fileno(), 1)
                os.dup2(err.fileno(), 2)
                rc = run_pipes([sys.executable, "-c", code], dict(os.environ), stdout_observer, stderr_observer)
            finally:
                os.dup2(old_out, 1)
                os.dup2(old_err, 2)
                os.close(old_out)
                os.close(old_err)
            out.seek(0)
            err.seek(0)
            self.assertEqual(out.read(), out_data)
            self.assertEqual(err.read(), err_data)
        self.assertEqual(rc, 7)
        self.assertEqual(classify("codex", events[0]).trigger, "rate_limit")

    def test_pty_input_resize_and_terminal_restoration(self):
        # Outer PTY simulates a real terminal; the integration opens its own inner PTY.
        master, slave = os.openpty()
        old_attrs = termios.tcgetattr(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
        child_code = (
            "import os,signal,sys; "
            "signal.signal(signal.SIGWINCH,lambda *a:print('RESIZED:'+str(os.get_terminal_size().columns),flush=True)); "
            "print('READY',flush=True); line=input(); print('RECEIVED:'+line,flush=True); sys.exit(3)"
        )
        harness = (
            "import sys,os;sys.path.insert(0," + repr(str(ROOT / 'src')) + ");"
            "from agent_pushover.stream import run_pty,TerminalObserver;"
            "from agent_pushover.config import DEFAULTS;"
            "r=run_pty([sys.executable,'-c'," + repr(child_code) + "],dict(os.environ),TerminalObserver(lambda e:None,DEFAULTS),lambda:None);"
            "print('EXIT:'+str(r),flush=True)"
        )
        proc = subprocess.Popen([sys.executable, '-c', harness], stdin=slave, stdout=slave, stderr=slave,
                                start_new_session=True)
        received = bytearray()
        def wait_for(marker):
            deadline = time.monotonic() + 8
            with selectors.DefaultSelector() as sel:
                sel.register(master, selectors.EVENT_READ)
                while marker not in received and time.monotonic() < deadline:
                    for _, _ in sel.select(0.1):
                        try:
                            chunk = os.read(master, 65536)
                        except OSError:
                            chunk = b''
                        if chunk:
                            received.extend(chunk)
                self.assertIn(marker, received, bytes(received))
        try:
            wait_for(b'READY')
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 120, 0, 0))
            os.kill(proc.pid, signal.SIGWINCH)
            wait_for(b'RESIZED:120')
            os.write(master, b'hello agent\n')
            wait_for(b'RECEIVED:hello agent')
            wait_for(b'EXIT:3')
            self.assertEqual(proc.wait(timeout=5), 0)
            self.assertEqual(termios.tcgetattr(slave), old_attrs)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)
            os.close(slave)


if __name__ == '__main__':
    unittest.main()
