# Examples and safe event replay

All event IDs, paths, questions, messages, and commands below are synthetic. They illustrate the fields the adapter consumes, not a recording of a real agent session. A real CLI can supply additional fields.

From the repository root, preview without configuration changes or network requests:

```sh
python3 -I src/entry.py replay --platform claude examples/events/claude-rate-limit.json --dry-run
python3 -I src/entry.py replay --platform codex examples/events/codex-tool-error.json --dry-run
```

After installation, the installed launcher also accepts these commands. A dry run prints the sanitized message or `null` for an ignored event. It previews classification/rendering only; it does not exercise queue deduplication, timers, native hook dispatch, or phone delivery.

Removing `--dry-run` enqueues a **real notification** using the installed credentials. Do not replay a directory of fixtures indiscriminately: it can send many messages.

`config.example.json` contains the full default configuration. The installer creates the real config automatically; it does not need this file copied into place. Change settings with `agent-pushover config set`.

## Fixture index

| File | Expected result |
| --- | --- |
| `claude-stop.json` | Complete |
| `claude-permission.json` | Idle / approval |
| `claude-question.json` | Idle / decision |
| `claude-plan.json` | Idle / plan approval |
| `claude-idle.json` | Idle |
| `claude-rate-limit.json` | Rate limit |
| `claude-api-error.json` | Error |
| `claude-tool-error.json` | Error |
| `claude-interrupted-tool.json` | Ignored intentional interruption |
| `codex-stop.json` | Complete; native replay can also schedule a derived idle reminder |
| `codex-permission.json` | Idle / approval |
| `codex-question.json` | Idle / decision |
| `codex-tool-error.json` | Error |
| `codex-notify-legacy.json` | Complete, using the completion-only legacy shape |
| `codex-successful-read.json` | Ignored despite error-related source text in output |

`codex-exec.jsonl` is a synthetic multi-record structured stream for `JsonObserver`, not a hook payload. The `replay` command accepts one JSON object, so it does not consume this JSONL file. The automated stream tests demonstrate incremental parsing.

No example contains a Pushover credential. Set real credentials through the installer’s hidden prompts or your environment/secret manager; do not commit them.
