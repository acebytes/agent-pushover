# Verification and live acceptance

## Executed here

Date: **2026-09-28**

Environment: Linux x86_64, Python 3.13.5, SQLite 3.46.1.

Commands:

```sh
python3 -m compileall -q src tests
python3 -m unittest discover -s tests -v
sh ./install.sh --help
```

Result: **98 tests passed**, zero failures or skips, in 29.472 seconds for the recorded suite run. See [test-results.txt](test-results.txt) for the complete captured unittest output. Timing is a measurement of that run, not an installation or delivery promise.

The suite includes:

- Event mapping and priority: completion, questions/plans/permissions, notifications, API/tool failures, quota detection, interruptions, and negative examples.
- Privacy: default omission of details, credential redaction, message bounds, private permissions, and no raw details in queue/log defaults.
- Queue: persistence, per-session/per-turn dedupe, disabled triggers, retry budget, permanent delivery failures, expiration, idle-timer cancellation, and concurrent identical events.
- Installer: actual install/reinstall/uninstall in temporary directories, including spaces and apostrophes; preservation of unrelated hooks and later edits; byte-for-byte preservation of Codex `config.toml`/legacy `notify`; rejected malformed/symlink/unowned files; environment-only credentials.
- Wrapper: incremental ANSI and UTF-8 handling, redraw coalescing, bounded buffering, structured JSON event parsing, byte-exact piped stdout/stderr and exit status, and an actual nested pseudo-terminal exercising input forwarding, resizing, and terminal restoration.
- Worker: 18 concurrent real hook subprocesses and actual detached workers using a fake transport; all 18 distinct messages delivered once in that test, with an empty final queue. This is a tested scenario, not an exactly-once guarantee for real HTTPS.
- Pushover client: mocked request encoding, validation endpoint, response-status handling, retry classification, refusal of redirects, and rejection of unsupported endpoints.

All test credentials are fake. Network transport is mocked/replaced; tests do not send real pushes or depend on an actual Claude/Codex login. Temporary home directories isolate tests from user configuration. Fixture dry-run previews were also checked.

## Not executed here

No authenticated Claude Code or Codex session was available for live event capture. No real Pushover credentials were supplied, so no receiving phone or real API delivery was tested. macOS, WSL, other Python versions, terminal multiplexers, managed enterprise environments, and the included GitHub Actions matrix were not run here. Those are explicit follow-up acceptance targets, not completed certifications.

## Live acceptance checklist

Run these checks on the actual machine/account before relying on alerts while away. Use a disposable working directory and benign tasks. Do not intentionally exhaust paid usage quotas or trigger destructive actions just to test a notifier.

1. Run the installer. Confirm that its test is accepted and appears on the intended device. A successful API result alone is not proof of phone receipt.
2. Restart both CLIs. In Codex, review and trust the hook entries in `/hooks`. Run `agent-pushover doctor`; verify the CLI versions and correct configuration paths.
3. In Claude Code, request a harmless short response and confirm one completion alert. Leave the resulting prompt untouched to exercise its idle notification.
4. In each CLI, use a benign action that your current permission policy genuinely asks you to approve. Confirm an attention alert and verify that nothing was auto-approved. Also exercise an explicit human question/plan when supported.
5. Start Codex through `codex-pushover`. After a turn stops, leave it alone for the configured reminder interval. On a separate turn, type before the interval; verify that observed activity cancels that reminder.
6. Use a controlled benign command failure, such as a deliberately failing test in a scratch project, to verify native tool-error handling. Do not assume that a tool failure must end the agent’s entire task.
7. Replay `claude-rate-limit.json` or another fixture without `--dry-run` to test the classification → queue → API path. This is a synthetic test, not proof that the CLI emits that event in your environment. Check real usage-limit behavior when it occurs naturally.
8. Run `codex-pushover exec --json` with a small harmless prompt. Confirm machine-readable stdout is intact and completion alerts arrive. Check structured error handling with a safe failure condition supported by your setup; the offline suite already exercises the event shapes.
9. Disconnect delivery temporarily or use an isolated test configuration to inspect local failure logging. Avoid changing production credentials during active sessions. Reconnect and run `drain --wait`; note the configured retry budget/TTL.
10. Disable a trigger via `config set`, verify it no longer sends, then restore it. Confirm priorities and device quiet-hour behavior suit your preferences.
11. Uninstall with `--purge`, restart the CLIs, and verify unrelated settings still work and new alerts stop. The disabled marker/coordination files intentionally remain without secrets.

Record observed CLI versions and any missing or changed payload fields before modifying event mappings. Share sanitized synthetic reproductions, not real transcripts or credential files.

## Re-running or publishing

Run the standard-library test command from the repository root. The optional GitHub Actions workflow runs Linux/macOS with Python 3.11–3.13 after publication. It requires no secrets. Add platform/version-specific regression tests before broadening support claims.
