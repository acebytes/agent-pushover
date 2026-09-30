# Changelog

## 1.0.0 — 2026-09-28

Initial source release.

- One installer for Claude Code and Codex native hooks; private credentials and optional environment-only operation.
- Completion, attention, rate-limit, and error classification with per-trigger toggles/priorities.
- Optional POSIX terminal wrappers and a structured Codex `exec --json` observer.
- Durable bounded queue, session-aware deduplication, derived Codex idle reminder, and serialized on-demand delivery.
- Config-preserving updates/uninstall, offline diagnostics/replay, sample events, and automated tests.
- Explicit limitations: no automatic Codex trust, native Windows implementation, universal error guarantee, remote replies, or live-device certification.
