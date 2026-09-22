# Native Retry Equivalence Report

## Verified request properties

- Original thread ID; no conversation creation.
- Exactly `input:[]`; no original prompt or synthetic user message.
- Official `turn/start` method.
- No optional model, permission, sandbox, collaboration, workspace, or agent overrides.

## Supporting evidence

The upstream empty-input test checks that no empty user message is synthesized. A local capacity failure recovered through the companion produced an accepted response followed by a new turn in the same thread. Another terminal capacity failure entered the next backoff attempt. The scheduler unit tests verify the live sequence `0, 3, 5, 10, 15, 30, 60...`, jitter bounds, and reset after a successful continuation/new task.

The latest failed turn is checked before each write, including failures that predate watcher startup. A `systemError` thread is allowed only after that capacity check.

## Differences and unknowns

- The CLI has no Desktop Retry button capture to compare. This is an app-server continuation report, not a claim of fully captured Desktop equivalence.
- Desktop research identified capacity-specific trigger labels; the CLI request does not copy those unverified labels.
- Rollout completion records do not always preserve `willRetry`; the terminal-capacity exception is described in [protocol notes](protocol-notes.md).
- Read/check/write is not atomic. There is a race with external clients, and in-memory deduplication does not cover multiple watchers or crashes.
- Omitting settings avoids deliberate overrides; preservation of every Goal, output-schema, multi-agent, and per-turn setting has not been exhaustively verified.
- Native continuation does not replay the original turn. It cannot prove the model will never choose to repeat a previously completed side effect.

No transcript or raw IPC capture is included in the public repository.
