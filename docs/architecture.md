# Architecture

```text
rollout JSONL -> metadata parser -> capacity classifier -> recovery scheduler
                                                   |-> dry-run decision
                                                   |-> shared local app-server
                                                       read thread + latest turn
                                                       turn/start input=[]
```

The Python standard library implementation contains the rollout observer, in-memory recovery engine, official proxy/WebSocket adapter, diagnostics, and command-line entry points.

The observer considers existing final capacity failures at startup and tails new records. The engine deduplicates thread/failed-turn pairs, cancels obsolete episodes, and applies bounded exponential backoff with jitter. Only acknowledged native continuation IDs carry an attempt budget forward.

Before a live write, the adapter checks loaded status and the latest failed capacity turn. Active, unloaded, mismatched, or unknown states stop that episode. Ambiguous transport results are not resent.

State and small metadata logs live under the current user's application-data directory. The normal CLI path never uses UI Automation. There is no tray executable, cross-process lock, persistent retry ledger, or atomic server-side compare-and-start guard in this version.
