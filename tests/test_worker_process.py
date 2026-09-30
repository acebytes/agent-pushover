"""Exercise detached worker startup/queue coordination without making network calls."""
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import ROOT, configured_home, jobs
from agent_pushover.engine import lock


class WorkerProcessTests(unittest.TestCase):
    def test_real_detached_workers_deliver_concurrent_hooks_once(self):
        with tempfile.TemporaryDirectory(prefix="worker-test-") as directory:
            home = configured_home(directory)
            runtime = home / "runtime"
            shutil.copytree(ROOT / "src", runtime, ignore=shutil.ignore_patterns("__pycache__"))
            # Replace only the transport in this temporary runtime. No real API endpoint is contacted.
            entry = runtime / "entry.py"
            code = entry.read_text()
            insertion = '''
from agent_pushover import pushover
def _fake_send(credentials, message, config):
    import json
    home = Path(sys.argv[sys.argv.index("--home") + 1])
    with (home / "test-sent.jsonl").open("a") as f:
        f.write(json.dumps(message) + "\\n")
pushover.send = _fake_send
'''
            entry.write_text(code.replace("from agent_pushover.cli import main", insertion + "\nfrom agent_pushover.cli import main"))
            env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_PUSHOVER_")}
            def invoke(number):
                event = {"hook_event_name": "Stop", "session_id": f"session-{number}", "cwd": "/repo/test"}
                result = subprocess.run([sys.executable, "-I", str(entry), "--home", str(home),
                                         "hook", "--platform", "claude"], input=json.dumps(event),
                                        text=True, capture_output=True, env=env, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                list(executor.map(invoke, range(18)))
            deadline = time.monotonic() + 10
            path = home / "test-sent.jsonl"
            while time.monotonic() < deadline:
                count = len(path.read_text().splitlines()) if path.exists() else 0
                if count == 18 and not jobs(home):
                    break
                time.sleep(0.05)
            self.assertEqual(len(path.read_text().splitlines()), 18)
            self.assertEqual(jobs(home), [])
            with lock(home / "worker.lock"):
                pass


if __name__ == "__main__":
    unittest.main()
