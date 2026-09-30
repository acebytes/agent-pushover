import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT
from agent_pushover.install import EVENTS, hook_document, remove_hooks


class MergeTests(unittest.TestCase):
    def test_merge_keeps_other_settings(self):
        original = {"model": "my-model", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}
        new = hook_document(original, "claude", "ours")
        self.assertEqual(new["model"], "my-model")
        self.assertEqual(new["hooks"]["Stop"][0]["hooks"][0]["command"], "other")
        self.assertEqual(len(new["hooks"]["Stop"]), 2)
        self.assertEqual(len(original["hooks"]["Stop"]), 1)

    def test_idempotent_hook_merge(self):
        once = hook_document({}, "codex", "ours")
        twice = hook_document(once, "codex", "ours", ["ours"])
        self.assertEqual(once, twice)

    def test_uninstall_preserves_shared_matcher_group(self):
        data = {"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "ours"},
                                                                     {"type": "command", "command": "other"}]}]}}
        new = remove_hooks(data, {"ours"})
        self.assertEqual(new["hooks"]["Stop"][0]["hooks"], [{"type": "command", "command": "other"}])

    def test_codex_never_receives_claude_only_events(self):
        new = hook_document({}, "codex", "ours")
        for event in ("Notification", "StopFailure", "PostToolUseFailure", "PermissionDenied", "Elicitation"):
            self.assertNotIn(event, new["hooks"])

    def test_invalid_hooks_rejected(self):
        for obj in ({"hooks": []}, {"hooks": {"Stop": {}}}, {"hooks": {"Stop": ["not-a-group"]}}):
            with self.assertRaises(ValueError):
                hook_document(obj, "claude", "ours")


class InstallerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent pushover's ")
        self.base = Path(self.temp.name)
        self.user_home = self.base / "user home"
        self.user_home.mkdir()
        self.home = self.base / "private app"
        self.bin = self.base / "bin dir"
        self.claude = self.base / "claude config"
        self.codex = self.base / "codex config"
        self.env = {**os.environ, "HOME": str(self.user_home), "CLAUDE_CONFIG_DIR": str(self.claude),
                    "CODEX_HOME": str(self.codex), "PUSHOVER_USER_KEY": "u" * 30, "PUSHOVER_APP_TOKEN": "t" * 30}
        self.env.pop("AGENT_PUSHOVER_HOME", None)
        self.env.pop("AGENT_PUSHOVER_RUN_ID", None)
        self.env.pop("AGENT_PUSHOVER_STREAM", None)

    def tearDown(self):
        self.temp.cleanup()

    def install(self, *extra):
        return subprocess.run([str(ROOT / "install.sh"), "--home", str(self.home), "--bin-dir", str(self.bin),
                               "--skip-validate", "--skip-test", "--non-interactive", *extra],
                              env=self.env, text=True, capture_output=True, timeout=15)

    def uninstall(self, *extra):
        return subprocess.run([str(ROOT / "install.sh"), "--home", str(self.home), "--uninstall", *extra],
                              env=self.env, text=True, capture_output=True, timeout=15)

    def read(self, path):
        return json.loads(path.read_text())

    def test_one_command_installs_and_uninstalls_both_with_spaces(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ACTION REQUIRED", result.stdout)
        self.assertTrue((self.home / "runtime" / "entry.py").is_file())
        for p, directory in (("claude", self.claude), ("codex", self.codex)):
            file = directory / ("settings.json" if p == "claude" else "hooks.json")
            self.assertEqual(set(self.read(file)["hooks"]), set(EVENTS[p]))
            command = self.read(file)["hooks"]["Stop"][0]["hooks"][0]["command"]
            # Malformed JSON avoids outbound delivery while exercising the installed command path.
            hook = subprocess.run(command, shell=True, input="not-json", text=True, env=self.env,
                                  capture_output=True, timeout=10)
            self.assertEqual(hook.returncode, 0, hook.stderr)
            self.assertEqual(hook.stdout, "{}\n" if p == "codex" else "")
        diagnostic = subprocess.run([str(self.bin / "agent-pushover"), "config", "show"],
                                    text=True, capture_output=True, env=self.env, timeout=10)
        self.assertEqual(diagnostic.returncode, 0, diagnostic.stderr)
        self.assertIn('"complete"', diagnostic.stdout)
        self.assertEqual((self.home / "credentials.json").stat().st_mode & 0o777, 0o600)
        result = self.uninstall("--purge")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.claude / "settings.json").exists())
        self.assertFalse((self.codex / "hooks.json").exists())
        self.assertFalse((self.home / "credentials.json").exists())
        self.assertFalse((self.home / "runtime").exists())
        self.assertFalse((self.bin / "agent-pushover").exists())

    def test_reinstall_and_later_user_edits_preserved_on_uninstall(self):
        self.claude.mkdir()
        original = {"model": "custom", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "original-hook"}]}]}}
        (self.claude / "settings.json").write_text(json.dumps(original))
        self.assertEqual(self.install().returncode, 0)
        once = self.read(self.claude / "settings.json")
        self.assertEqual(self.install().returncode, 0)
        twice = self.read(self.claude / "settings.json")
        self.assertEqual(once, twice)
        twice["another_setting"] = True
        twice["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "later-hook"}]})
        (self.claude / "settings.json").write_text(json.dumps(twice))
        result = self.uninstall()
        self.assertEqual(result.returncode, 0, result.stderr)
        remaining = self.read(self.claude / "settings.json")
        self.assertEqual(remaining["model"], "custom")
        self.assertTrue(remaining["another_setting"])
        self.assertEqual([g["hooks"][0]["command"] for g in remaining["hooks"]["Stop"]], ["original-hook", "later-hook"])
        self.assertTrue((self.home / "credentials.json").exists())
        self.assertTrue(list((self.home / "backups").glob("*.json")))

    def test_existing_codex_notify_preserved_byte_for_byte(self):
        self.codex.mkdir()
        toml = '# important comment\nnotify = ["old-notifier", "--option"]\nmodel = "custom"\n'
        (self.codex / "config.toml").write_text(toml)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.codex / "config.toml").read_text(), toml)
        self.assertEqual(self.uninstall().returncode, 0)
        self.assertEqual((self.codex / "config.toml").read_text(), toml)

    def test_invalid_json_aborts_without_replacing_settings(self):
        self.codex.mkdir()
        self.claude.mkdir()
        (self.claude / "settings.json").write_text('{"model":"preserve"}')
        (self.codex / "hooks.json").write_text("INVALID")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.codex / "hooks.json").read_text(), "INVALID")
        self.assertEqual((self.claude / "settings.json").read_text(), '{"model":"preserve"}')
        self.assertFalse((self.home / "runtime").exists())

    def test_disabled_codex_hooks_not_overridden(self):
        self.codex.mkdir()
        toml = "[features]\nhooks = false\n"
        (self.codex / "config.toml").write_text(toml)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("explicitly disabled", result.stderr)
        self.assertEqual((self.codex / "config.toml").read_text(), toml)
        self.assertFalse((self.claude / "settings.json").exists())

    def test_unowned_binary_never_overwritten(self):
        self.bin.mkdir()
        (self.bin / "agent-pushover").write_text("my unrelated program")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.bin / "agent-pushover").read_text(), "my unrelated program")

    def test_symlink_config_never_replaced(self):
        self.claude.mkdir()
        actual = self.base / "actual-settings.json"
        actual.write_text("{}")
        (self.claude / "settings.json").symlink_to(actual)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.claude / "settings.json").is_symlink())
        self.assertEqual(actual.read_text(), "{}")

    def test_env_only_does_not_persist_credentials(self):
        result = self.install("--env-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / "credentials.json").exists())
        combined = (self.claude / "settings.json").read_text() + (self.codex / "hooks.json").read_text()
        self.assertNotIn("u" * 30, combined)
        self.assertNotIn("t" * 30, combined)

    def test_single_platform_install(self):
        result = self.install("--platform", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.claude / "settings.json").exists())
        self.assertFalse((self.codex / "hooks.json").exists())


if __name__ == "__main__":
    unittest.main()
