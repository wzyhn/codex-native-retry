# Native retry research

## Scope and evidence

This is a CLI companion. Initial investigation covered Windows Desktop and CLI, then narrowed to the public local app-server transport. The tested runtime was `0.154.0-alpha.6.2`, with a `0.155.1` CLI client. Version numbers describe observations, not a permanent compatibility guarantee.

The [official app-server documentation](https://developers.openai.com/zh-Hans/docs/app-server) documents local `unix://` connections, `thread/read`, `thread/loaded/list`, `thread/turns/list`, and `turn/start`.

## Findings

- Normal CLI sessions can own private stdio servers. A newly started server cannot safely take over those pipes.
- `codex app-server --listen unix://` exposes a shared local control socket. `codex --remote unix:// resume` connects a CLI to it.
- A managed standalone installation can start a daemon. Some other distributions lack the managed installation layout; direct `app-server --listen unix://` is the alternative.
- The companion uses the official `app-server proxy --sock` byte transport with a WebSocket handshake; it does not guess Desktop coordination-pipe RPCs.
- `ErrorNotification` carries `threadId`, `turnId`, `error`, and `willRetry`. The tool leaves `willRetry=true` to Codex.
- Terminal rollout `task_complete` records may omit `willRetry`. Only a recognized terminal capacity failure is treated as finished in that shape.
- After capacity failure a loaded thread can report `systemError`, rather than `idle`. Treating every non-idle state as active incorrectly prevents recovery.
- Before sending, `thread/turns/list` with `limit:1`, descending order, and `itemsView:notLoaded` must return the same failed capacity turn. No transcript items are requested.

The continuation request contains only the original thread ID and an empty input array. Optional state overrides are omitted.

## Source evidence

- [Protocol definitions](https://github.com/openai/codex/tree/main/codex-rs/app-server-protocol/src/protocol/v2)
- [Thread status implementation](https://github.com/openai/codex/blob/main/codex-rs/app-server/src/thread_status.rs)
- [Failed-thread status tests](https://github.com/openai/codex/blob/main/codex-rs/app-server/tests/suite/v2/thread_read.rs)
- [Empty-input turn tests](https://github.com/openai/codex/blob/main/codex-rs/app-server/tests/suite/v2/turn_start.rs)

The official empty-input test asserts that the request does not synthesize an empty user message. Links follow upstream development; local capabilities must still be checked.

## Local validation, with private data omitted

Read-only initialization and thread listing succeeded through the proxy. After correcting the `systemError` guard, a previously failed capacity turn received `turn_start_accepted`, followed by a new turn in the same thread. A subsequent capacity failure carried the attempt budget into the next backoff.

No raw thread identifiers, conversation contents, workstation paths, private repository details, or raw IPC captures are included here. A complete Desktop-button-versus-companion capture comparison was not performed. The supported CLI route is intentionally narrower. See the [equivalence report](native-retry-equivalence.md) for the limits of this evidence.
