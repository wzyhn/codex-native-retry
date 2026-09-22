# Architecture

```text
Codex rollout JSONL -> metadata parser -> capacity classifier -> recovery scheduler
                                                            |-> dry-run decision
                                                            |-> shared local app-server
                                                                read thread + latest turn
                                                                turn/start input=[]

start command -> managed daemon or bundled app-server -> one background watcher
```

The Python standard library implementation contains the rollout observer, in-memory recovery engine, official proxy/WebSocket adapter, diagnostics, and command-line entry points. `start` performs the service bootstrap and launches the watcher detached with local log files; it never changes Codex configuration or injects into a process.

The observer considers existing final capacity failures at startup and tails new records. The engine deduplicates thread/failed-turn pairs, cancels obsolete episodes, and applies the explicit consecutive-failure schedule `0, 3, 5, 10, 15, 30, 60...` with jitter and a 60-second cap. Only an acknowledged native continuation ID carries that attempt count to the next turn; a new task has no association and starts immediately.

Before a live write, the adapter checks loaded status and the latest failed capacity turn. Active, unloaded, mismatched, or unknown states stop that episode. Ambiguous transport results are not resent.

State and small metadata logs live under the current user's application-data directory. The normal CLI path never uses UI Automation. There is no tray executable, cross-process lock, persistent retry ledger, or atomic server-side compare-and-start guard in this version.
