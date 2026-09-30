# Agent Pushover

**Phone notifications for Claude Code and Codex CLI, with one installer.**

Get an alert when a turn finishes, approval or input is needed, a rate/usage limit is reported, or a tool/runtime error is detected. Notifications are passive: this integration never approves a command, answers a question, changes sandbox policy, or tells an agent to keep working.

This is an independent, MIT-licensed integration—not an official Anthropic, OpenAI, or Pushover product. It uses native hooks where available and includes optional CLI wrappers for otherwise-unobservable errors. **No universal “every error” event exists across these two CLIs.** The coverage boundary is documented rather than hidden.

## Requirements and compatibility

- **Python 3.11 or newer**, including its standard-library SQLite support. No pip dependencies, Node packages, `jq`, Docker, or service manager are needed.
- **macOS, Linux, or WSL**, with a POSIX shell. Native Windows/PowerShell is not supported by this version. Use the Linux CLIs and Linux Python inside WSL, not `codex.exe` from Windows.
- A current Claude Code and/or Codex CLI supporting the hooks in [the capability matrix](docs/API-CAPABILITIES.md). Historical completion-only Codex releases are not the installer’s target. `doctor` reports your installed versions; it cannot prove their runtime event coverage.
- A [Pushover account](https://pushover.net/), a registered receiving device, your user key (or group key), and an application API token created in your Pushover dashboard. Use a dedicated application token for these alerts. See the [Pushover API](https://pushover.net/api).
- Outbound HTTPS from the machine actually running the CLI to `api.pushover.net`. On a remote machine, container, or WSL instance, install there. The computer and agent must stay running; a sleeping/offline machine cannot deliver immediately.

APIs were checked against primary documentation on **2026-09-28**. Automated tests were executed on Linux; see [testing status and the live acceptance checklist](docs/TESTING.md). macOS/WSL compatibility is a design target, not a claim of completed live testing.

## One-command installation

Extract the repository ZIP, open a terminal in the extracted `agent-pushover` directory, and run:

```sh
sh ./install.sh
```

That is the entire configuration installer. It prompts privately for the two credentials, validates them with Pushover, installs its runtime, merges both sets of hooks, creates launchers, and sends an installation test. No hook JSON needs to be copied or edited manually. It does not require `sudo` and does not install the agent CLIs themselves.

**Activation still requires restarting existing agent processes. In Codex, open `/hooks` and review/trust the installed hooks.** Codex deliberately requires this review; installing a plugin would not eliminate it. This installer never writes trust hashes, bypasses review, enables explicitly disabled hooks, or overrides organization policy. A test push confirms the sender, not that an agent has activated its hooks.

The default launchers are:

```text
~/.local/bin/agent-pushover
~/.local/bin/claude-pushover
~/.local/bin/codex-pushover
```

Hooks contain absolute paths and work even when `~/.local/bin` is not on your `PATH`. Use the paths above directly, or the short command names when that directory is already on `PATH`. The installer intentionally does not edit shell profiles or replace the original `claude`/`codex` executables.

### Environment credentials and install options

Existing `PUSHOVER_USER_KEY` and `PUSHOVER_APP_TOKEN` variables take precedence over saved credentials. With both already exported by your shell or secret manager:

```sh
sh ./install.sh --non-interactive
```

By default, credentials are saved in a private local file so subsequent sessions need no environment setup. To require environment-only credentials instead:

```sh
sh ./install.sh --non-interactive --env-only
```

Both variables must then be present in **every agent session**, and they should identify the same Pushover destination for a shared installation. `--env-only` removes any previously saved credentials. Prefer secret-manager injection or the hidden prompts over placing real secrets in shell history.

Other supported options:

| Option | Meaning |
| --- | --- |
| `--platform claude` / `--platform codex` | Install only that integration. Default: both, even when a CLI is not installed yet. |
| `--home /absolute/private/path` | Use a dedicated integration directory. Never use your home directory itself. |
| `--bin-dir /absolute/bin/path` | Set launcher destination. |
| `--skip-validate --skip-test` | Make installation offline. Keys must still have the correct format; delivery remains unverified. |
| `--uninstall --purge` | Remove this installation and its private data. |

`PYTHON=/path/to/python3.11 sh ./install.sh` selects the interpreter. The installed launchers keep that absolute interpreter path; reinstall after removing or relocating it. `CLAUDE_CONFIG_DIR` and `CODEX_HOME` select agent configuration directories. `AGENT_PUSHOVER_HOME` or `XDG_CONFIG_HOME` can select the integration directory. Use the same locations when reinstalling.

Re-running the installer updates its runtime and replaces **only its own recorded hook commands**. Unrelated hooks and settings survive. Existing Codex `notify` configuration is not changed. Agent JSON files are reformatted on a successful merge; originals are backed up privately. Stop active sessions before an upgrade to avoid a cached hook loading code during replacement.

## Everyday use

After activation, launching ordinary `claude` or `codex` uses the installed native hooks automatically.

For the widest available detection, launch these wrappers instead:

```sh
~/.local/bin/claude-pushover
~/.local/bin/codex-pushover
```

They pass the original arguments through without a shell and preserve normal permission settings. Interactive sessions use a pseudo-terminal; noninteractive sessions retain separate stdout and stderr streams. Terminal dimensions and common termination signals are forwarded. The wrapper observes output without saving a transcript and notices input activity without logging its bytes.

**Use `codex-pushover` rather than ordinary `codex` when interactive API/rate-limit errors matter.** Codex’s native hook coverage alone is insufficient for those. `claude-pushover` can similarly catch visible startup/runtime failures that happen outside Claude’s hook lifecycle.

For noninteractive Codex, request its structured stream explicitly:

```sh
~/.local/bin/codex-pushover exec --json "Summarize this repository"
```

The JSONL stdout remains unchanged, so it can still be redirected or consumed by another program. The wrapper recognizes structured `error`, `turn.failed`, `turn.completed`, and supported failed tool items instead of guessing from ordinary assistant prose. This mode does not schedule a post-completion idle reminder. The wrapper does **not** silently add `--json` or change approval/sandbox flags.

## What produces a notification?

The exact event mapping, exclusions, primary-source links, and native-versus-derived distinction are in [API-CAPABILITIES.md](docs/API-CAPABILITIES.md).

| Trigger | Claude Code adapter | Codex adapter | Default priority |
| --- | --- | --- | --- |
| Complete | Normal `Stop` | Native `Stop`; structured completion in `exec --json` | `0` |
| Idle / human attention | Permission, question/plan, elicitation, and selected `Notification` events | Permission/question hooks; derived post-stop reminder; matching terminal prompts | `0` |
| Rate / usage limit | Failure events and explicit error text classified as a limit | Failed tool results; wrapper output or structured execution errors | `1` |
| Other error | API/tool failure events and explicit failed results | Failed tool results; wrapper errors and nonzero process exit | `1` |

“Complete” means **a turn stopped**, not that the whole project succeeded. Failed tool calls can be recoverable, so an error alert may be followed by a normal completion. Permission/question hooks indicate that human interaction was requested; another hook or the user may answer before the push arrives. All error conditions are enabled by default, including recoverable tool failures; this can be noisy.

By default, notifications contain a category-specific summary, project folder name, hostname, short session identifier, and event name—not prompt text or command output. Examples:

```text
Claude Code · Approval needed · my-project
A permission request is waiting for you.

workstation · session 1234abcd · PermissionRequest
```

```text
Codex · Rate / usage limit · my-project
A rate or usage limit was reported; check the session before resuming.

workstation · session 5678abcd · RuntimeError
```

Pushover priority `1` is **high priority and bypasses quiet hours**. Set errors/limits to `0` to avoid that. This integration permits `-2`, `-1`, `0`, and `1`; it deliberately rejects emergency priority `2`, which has repeated-delivery requirements. See [Pushover’s priority documentation](https://pushover.net/api#priority).

## Configuration without editing files

Inspect effective configuration:

```sh
~/.local/bin/agent-pushover config show
```

Change a single setting:

```sh
# Disable completion alerts, leaving attention/failure alerts enabled.
~/.local/bin/agent-pushover config set triggers.complete.enabled false

# Respect quiet hours for errors and rate limits.
~/.local/bin/agent-pushover config set triggers.error.priority 0
~/.local/bin/agent-pushover config set triggers.rate_limit.priority 0

# Disable only the derived Codex post-stop reminder, not approvals/questions.
~/.local/bin/agent-pushover config set codex.idle_after_stop_seconds 0
```

Every trigger (`complete`, `idle`, `rate_limit`, `error`) has `enabled`, `priority`, and `dedupe_seconds`. Default duplicate-suppression intervals are 5, 8, 120, and 30 seconds respectively. Suppression is scoped by agent/session and trigger, not one global throttle. Distinct error details get separate keys; rate-limit bursts share a key. Two distinct approval requests in one session within the idle cooldown can be coalesced. A new turn gets a separate completion key.

Useful additional settings:

```sh
# Include best-effort-redacted command/question/error snippets in pushes.
~/.local/bin/agent-pushover config set privacy.include_details true

# Omit identifying machine/project metadata.
~/.local/bin/agent-pushover config set privacy.include_host false
~/.local/bin/agent-pushover config set privacy.include_project false

# Optionally target a registered device by name.
~/.local/bin/agent-pushover config set pushover.device 'my-phone'

# Disable heuristic output scanning (native hooks and JSON events still work).
~/.local/bin/agent-pushover config set terminal.enabled false

# Add a locally trusted message pattern for a CLI's changed/localized wording.
~/.local/bin/agent-pushover config set terminal.extra_rate_limit_patterns '["^LIMIT REACHED!"]'
```

Additional regex arrays are `terminal.extra_error_patterns` and `terminal.extra_idle_patterns`. Keep custom expressions simple: Python regexes have no execution deadline. Wrapper pattern settings are loaded when the wrapper starts; restart it after changing them. Hook settings reload per event; the sender rechecks trigger toggles and priorities before delivery. Already-queued message text is not rewritten by privacy changes, so review privacy settings **before** starting sessions.

[examples/config.example.json](examples/config.example.json) contains every default. Credentials are stored separately and are never shown by `config show`.

### The Codex idle reminder

This integration schedules a reminder 60 seconds after a native Codex `Stop`. It cancels the reminder when observed input, a submitted prompt, a new tool call, a session end, or a terminal/API failure indicates the session is no longer waiting.

Without the wrapper, native events reveal submitted prompts—not every keystroke—so typing an unfinished prompt can still result in a reminder. With the wrapper, input activity cancels it. It is a **post-stop approximation**, not a native Codex idle event and not a timer that mistakes silent computation for waiting. No native Stop/trusted hooks means no derived reminder. A long-running background worker can coexist with a stopped main turn; review the session rather than assuming everything has halted.

## Delivery, reliability, and privacy

Hook processes normalize and enqueue events quickly; a detached, on-demand sender does the HTTPS work. There is no permanently installed daemon, cron job, launch agent, or systemd unit. One sender per installation holds a lock and exits after its pending deliveries/reminders finish. It may stay alive during a delayed reminder or retry.

By default, delivery uses a 5-second network timeout, at most 3 attempts for transient network/server failures, a 500-item bounded queue, and a 1-hour maximum message age. Backoff is 2 then 10 seconds. Pushover rejection/HTTP 4xx, including Pushover’s own 429/quota response, is recorded locally rather than generating a recursive notification. There is no second alert channel when Pushover itself is unavailable.

Queued alerts survive a process crash, but this is **not an exactly-once or guaranteed-delivery system**. A timed-out POST can have been accepted and its retry may duplicate a push. A killed sender, system sleep, TTL expiry, queue overflow, configuration disablement, or inaccessible credentials can prevent delivery. Pending items resume on a new queued event or with:

```sh
~/.local/bin/agent-pushover drain --wait
```

Default private directory: `~/.config/agent-pushover` (or the selected override). It contains the runtime, config, credentials, SQLite queue, manifest, private config backups, and diagnostic log. Directory permissions are `700`, credential/state files are private, and hook commands contain no credential literals. Logs contain event categories/outcomes, not notification bodies. No terminal transcript or input keystrokes are retained. The queue does retain the rendered notification pending delivery.

**Details are off by default.** Enabling details sends selected text to Pushover and your device; redaction is best effort, not a guarantee against disclosure. Project/host names and a short session identifier are metadata too. Local permissions are not encryption or isolation from another process running as your account. See [SECURITY.md](SECURITY.md).

## Troubleshooting

Run these separately:

```sh
~/.local/bin/agent-pushover doctor
~/.local/bin/agent-pushover test
```

`doctor` is local-only. It checks known hook entries, CLI availability/versions, credential presence, queue size, and some explicit disable settings. It cannot attest Codex trust, enterprise policy, device delivery, or compatibility of every installed CLI version. `test` sends a real push and reports API acceptance—not receipt by a phone. Test any priority with `test --trigger error` or `test --trigger rate_limit`.

| Symptom | Check |
| --- | --- |
| Test push works, but Codex hooks do not | Restart Codex and review/trust the entries in `/hooks`. Also check local/managed hooks policy and `CODEX_HOME`. |
| Completion works but interactive Codex limits are missed | Start through `codex-pushover`. Native Codex hooks alone do not expose general API failures. |
| Claude hooks do not run | Restart Claude; inspect `/hooks`, hook disable settings, managed policy, and `CLAUDE_CONFIG_DIR`. Install a current hooks-capable version. |
| Installer refuses an existing file | Fix malformed JSON or an intentional symlink/launcher conflict. The installer refuses to guess or overwrite unrelated content. |
| Duplicate or noisy pushes | Check for another notification integration, existing Codex `notify`, other Stop hooks, recoverable tool failures, and heuristic terminal matches. Tune cooldowns/toggles. |
| Missing Pushover test | Check token/user/device, network/TLS, Pushover API quota, device app permissions, and quiet-hours settings. Run `test` for an explicit API error. |
| Queue stopped after shutdown | Run `drain --wait`; there is no automatic OS-startup service. Expired items are discarded. |
| Wrapper behaves differently in a terminal | Try the original CLI for native-hook-only operation. This is not a full terminal emulator; native Windows, all job-control sequences, and every terminal multiplexer are not certified. |

Inspect local diagnostic outcomes at the path printed by `doctor`. Never post credentials, config backups, message queues, or unredacted CLI transcripts in a public issue.

Preview a synthetic event without sending or writing state:

```sh
~/.local/bin/agent-pushover replay --platform claude examples/events/claude-rate-limit.json --dry-run
```

Removing `--dry-run` enqueues a **real notification** (subject to toggles and deduplication). See [sample events](examples/README.md).

## Uninstall

To remove hooks, launchers, runtime, credentials, queue, logs, and backups:

```sh
~/.local/bin/agent-pushover uninstall --purge
```

Or, from this extracted repository:

```sh
sh ./install.sh --uninstall --purge
```

For a custom directory, use `sh ./install.sh --home /your/private/path --uninstall --purge`.

Uninstall removes only recorded integration commands and unchanged managed launchers. It preserves unrelated hooks/settings, including edits made after installation. Locally modified launchers are left with a warning; remove or update those intentionally. A disabled marker and coordination lock files remain to keep old processes from re-enabling delivery; they contain no secrets. Restart running agent sessions because they may cache old hook commands.

Omit `--purge` to retain private credentials/config/queue/backups for reinstall. Choose purge on the uninstall operation itself: after uninstall, the manifest and executable are removed, so a second uninstall is not a separate purge command. Reinstalling and then uninstalling with `--purge` is the supported cleanup path in that case.

## Development and repository layout

```text
agent-pushover/
├── install.sh                     # one-command install/update/uninstall entry
├── src/
│   ├── entry.py                   # isolated Python launcher
│   └── agent_pushover/
│       ├── cli.py                 # commands and passive hook adapters
│       ├── config.py              # private configuration and redaction
│       ├── events.py              # classification and notification rendering
│       ├── engine.py              # durable queue, dedupe, timers, sender
│       ├── install.py             # reversible config merge and launchers
│       ├── pushover.py            # HTTPS API client
│       └── stream.py              # optional PTY/pipe and JSON observers
├── tests/                         # offline unit and subprocess integration tests
├── examples/                      # defaults and synthetic events
├── docs/                          # capabilities, verification, test report
├── .github/workflows/tests.yml    # Linux/macOS CI definition
├── README.md
├── SECURITY.md
├── THIRD_PARTY_NOTICES.md
└── LICENSE
```

Run the offline suite without installation:

```sh
python3 -m unittest discover -s tests -v
```

Tests use temporary homes and fake transport; they do not use your Pushover account or alter your agent configuration. The supplied CI workflow is ready to run after publishing, not evidence that remote CI has already passed. This repository is distributed as source; it has not been published to a marketplace by this installer.

The public repository [teito-dev/claudecode-pushover-integration](https://github.com/teito-dev/claudecode-pushover-integration) was reviewed as a reference. This project is an independent implementation using a private durable queue rather than its socket-daemon architecture. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
