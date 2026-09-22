# Protocol notes

## Native continuation

```json
{"method":"turn/start","params":{"threadId":"<existing-thread>","input":[]}}
```

No `clientUserMessageId`, prompt, `turnTrigger`, model, effort, cwd, workspace roots, collaboration mode, approvals, permissions, sandbox, service tier, personality, output schema, or agent settings are supplied. The companion depends on the existing thread's native handling of omitted overrides.

## Preconditions

1. Initialize the shared daemon through its official local proxy transport.
2. `thread/read` with `includeTurns:false` must return `idle` or `systemError` and must not explicitly disallow direct input.
3. `thread/turns/list` with `limit:1`, `sortDirection:desc`, `itemsView:notLoaded` must return the expected failed turn with a recognized capacity error.
4. Send one empty-input request. A complete response containing the new turn ID is accepted; transport ambiguity stops recovery.

`systemError` alone is never enough to authorize recovery. Likewise, a successful handshake only establishes transport availability; it does not mean a particular conversation can be recovered.

## Error policy

- Explicit `willRetry:true`: no additional retry.
- Terminal `willRetry:false` capacity: schedule.
- Missing `willRetry` on an app-server error: ignore.
- Missing `willRetry` on a terminal rollout `task_complete` capacity record: treat as terminal.
- Structured type takes priority; the exact capacity text is used only when no type exists.
- New turn or user activity cancels the previous episode.
- Usage limits, context-window, policy, and unknown failures do not trigger recovery.
- Consecutive capacity failures use `0, 3, 5, 10, 15, 30, 60...` seconds with bounded jitter; a successful continuation or new task resets the sequence.
- Recognized retry-after seconds may lengthen the local wait up to the configured 60-second cap.

The local control socket and experimental methods may change between Codex releases. Missing methods/fields stop recovery. This is runtime shape checking, not certification against every future protocol version.
