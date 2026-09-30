# Event mapping and support boundary

Verified against primary documentation on **2026-09-28**. This is an implementation mapping, not a claim that all historical CLI versions have the same event set. Keep this document and `install.EVENTS` together when updating integrations.

## Native input contracts

Both hook adapters receive one JSON object on stdin. The installer writes command-hook entries in the respective user configuration. Our Claude hook emits no stdout; our Codex hook emits `{}`. Both return zero on handled notification failures and never return approval, denial, continuation, or additional-context decisions.

### Claude Code mapping

| Installed event | Our interpretation |
| --- | --- |
| `Stop` | Complete, excluding payloads with a subagent `agent_id`. |
| `StopFailure` | Rate limit for `rate_limit`/recognized quota text; other API failures are errors. |
| `PermissionRequest` | Immediate approval-needed alert. |
| `Notification` | Known permission, idle, elicitation, and `agent_needs_input` types become attention alerts. Unknown types are ignored unless explicit failure text is recognized. |
| `PreToolUse`, matcher `AskUserQuestion\|ExitPlanMode` | Human question or submitted plan. |
| `PostToolUseFailure` | Error or limit; deliberate `is_interrupt` is ignored. |
| `PostToolUse` | Error only when the result contains an explicit supported failure signal. |
| `PermissionDenied` | Policy-denial error. |
| `Elicitation` | MCP input request. |
| `SessionStart`, `UserPromptSubmit`, `SessionEnd` | State bookkeeping, no notification by themselves. |

Native `StopFailure` replaces `Stop` for API-terminated turns. `PostToolUseFailure` does not cover pre-execution validation failures. Permission/idle notification timing depends on user activity, and background-input notifications are contextual, not universal. See the [Claude hooks reference](https://code.claude.com/docs/en/hooks).

Our classifier intentionally does not label arbitrary assistant prose as a failure. For example, completing a task that mentions “Error: rate limit reached” in an example should not become a rate-limit alert. `overloaded` is treated as a generic error unless the payload also contains a recognized usage-limit indication.

### Codex mapping

| Installed event | Our interpretation |
| --- | --- |
| `Stop` | Turn-complete alert; schedules an optional derived idle reminder. |
| `PermissionRequest` | Approval-needed alert. |
| `PreToolUse` | Cancels stale idle state; `request_user_input` is recognized when exposed as a local function-tool hook. |
| `PostToolUse` | Explicit failed results, including recognized nonzero exit status. |
| `SessionStart`, `UserPromptSubmit`, `SessionEnd` | State bookkeeping and reminder cancellation. |

Codex’s current documented hook list has no equivalent to Claude’s `Notification`, `StopFailure`, or `PostToolUseFailure`. Tool-hook coverage has exceptions; it is not a general error bus. The installer uses `hooks.json`, leaves `config.toml` untouched, and requires the user’s `/hooks` trust review. See the [Codex hooks reference](https://developers.openai.com/codex/hooks).

`request_user_input` mapping depends on the installed release exposing that local function through tool hooks; the request handler can be inspected in [OpenAI’s source](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/request_user_input.rs). No undocumented “idle” or “error” hook is invented by the installer.

## Workarounds shipped in this repository

### Structured Codex execution

`codex-pushover exec --json ...` maps `turn.completed` to complete and `error`/`turn.failed` to rate-limit or error. Supported failed command/MCP/file-change items are also inspected. Stdout is preserved byte-for-byte. Synthetic stream bookkeeping avoids a duplicate native-Stop completion and disables the derived interactive idle timer. This is a different execution mode, not an interactive TUI event feed. See [Codex noninteractive mode](https://developers.openai.com/codex/noninteractive).

### Terminal observation for either CLI

The wrappers normalize a bounded copy of output to detect explicit English error, usage-limit, and confirmation messages. Detection is incremental across byte/ANSI boundaries, but it is not a complete terminal emulator. Matching text is treated as a **reported** condition, not proof that work stopped. Custom patterns are optional.

Text that resembles an error in an example or tool output can cause a false positive. A changed message, localized text, truncation, unusual redraw, or a failure never printed can be missed. Normal successful tool output is not recursively scanned by native classifiers. The wrappers additionally report unexpected nonzero process exits, except conventional interrupt/termination statuses; a successful CLI exit alone is not assumed to mean a completed task.

### Derived idle, not a silence alarm

Codex’s reminder starts only after a detected Stop. New observed activity cancels it. Merely emitting no output during a long computation never schedules an idle notification. Without a wrapper, unsubmitted typing is not observable. See the configuration section of the main README for the exact timer behavior.

### Legacy completion adapter

`agent-pushover notify-codex '<event JSON>'` accepts the older `notify` argument protocol, including `agent-turn-complete`. It is provided for adaptation/testing but deliberately **not wired up by the installer**, which preserves an existing `notify` command unchanged. This protocol supplies completion only; TUI notifications do not turn it into a general external error/approval callback. See [Codex advanced configuration](https://developers.openai.com/codex/config-advanced).

## What is intentionally not automated

The installer does not create a Pushover account/application, install or authenticate either coding CLI, review Codex hooks, override managed security settings, keep a sleeping computer awake, or certify a phone’s notification settings. It does not publish a GitHub repository or install a marketplace plugin. Automatic approval/reply from Pushover is out of scope: this is a notification channel, not remote control.

Some detected failures are recoverable; some visible conditions have no native event. Therefore there is no defensible guarantee of “every error on every version.” Native hooks plus the wrapper are the shipped closest workaround. For a program embedding Codex rather than driving its CLI, the separately documented [Codex app-server](https://developers.openai.com/codex/app-server) offers another integration surface; no app-server client is included here.

## Source inventory

- [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)
- [Claude Code hooks guide](https://code.claude.com/docs/en/hooks-guide)
- [Codex hooks](https://developers.openai.com/codex/hooks)
- [Codex advanced configuration](https://developers.openai.com/codex/config-advanced)
- [Codex configuration reference](https://developers.openai.com/codex/config-reference)
- [Codex noninteractive mode](https://developers.openai.com/codex/noninteractive)
- [Codex developer commands](https://developers.openai.com/codex/cli/reference)
- [Pushover API](https://pushover.net/api)

The OpenAI developer URLs may redirect to the corresponding official ChatGPT Learn documentation. No third-party mirror is required for this implementation.
