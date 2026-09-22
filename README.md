# Codex Native Retry

[简体中文](README.zh-CN.md)

An **unofficial, experimental Windows companion for Codex CLI**. Recover terminal model-capacity failures through the existing thread's native empty-input continuation:

```json
{"method":"turn/start","params":{"threadId":"<existing-thread>","input":[]}}
```

No synthetic “continue” message, prompt replay, new conversation, or model/permission overrides. This project is not affiliated with or endorsed by OpenAI.

## Requirements

- Windows 10/11, Python 3.10+ (standard library only).
- A compatible Codex runtime and CLI sessions connected to a **shared local app-server**.
- Tested with runtime `0.154.0-alpha.6.2` and CLI `0.155.1`. Protocols change; other versions require verification.

A normal CLI session using its own stdio server cannot be attached to. The one-command launcher starts the shared server and watcher, but future conversations must connect with `--remote unix://`.

## Quick start

Clone this repository and open PowerShell in its root:

```powershell
git clone https://github.com/wzyhn/codex-native-retry.git
cd codex-native-retry
python .\src\codex_native_retry.py diagnose
.\start.ps1 -DryRun
```

Dry-run observes errors and records `would_retry` without starting a turn. For live recovery, run:

```powershell
.\start.ps1
```

This starts one shared local app-server (managed daemon when available, otherwise the bundled runtime) and one background watcher. The watcher tails the Codex home session directory, so it covers all threads visible through that shared daemon. It does not take over a CLI process that owns a private stdio server.

Use the shared daemon for each conversation:

```powershell
codex --remote unix:// resume
```

Close the old stdio CLI before opening the same conversation through `--remote`. A managed standalone installation can alternatively use `codex app-server daemon start`; `start.ps1` tries that route first.

If runtime discovery fails, pass the actual `codex.exe` path with `--runtime` to `start.ps1` or set `runtime_path` in the configuration. Discovery currently checks the Desktop runtime cache; an explicit CLI executable also works.

## When it acts

Only a terminal `serverOverloaded` / `server_overloaded` failure is eligible. The exact capacity message is a compatibility fallback only when no structured type is present.

- `willRetry=true`: Codex handles its own retry.
- Watcher startup also considers existing terminal capacity errors.
- Immediately before sending, the daemon must show `idle` or `systemError` and the **same latest failed turn**, still classified as capacity.
- An active/unloaded thread, new turn, manual retry, or new user message blocks the old episode.
- A transport timeout after sending stops the episode; an ambiguous request is never blindly retransmitted.
- Consecutive capacity failures use `0, 3, 5, 10, 15, 30, 60, 60...` seconds before jitter. A successful continuation or a new task starts again at zero seconds.

The continuation creates a new native turn inside the same thread. `accepted` means the server accepted the request; it does not guarantee model capacity is available or the task finishes.

## Settings and diagnostics

`install.ps1` creates `%APPDATA%\CodexNativeRetry\config.json`. These are the defaults:

```json
{
  "enabled": true,
  "dry_run": true,
  "retry_mode": "dry_run",
  "retry_delays_seconds": [0, 3, 5, 10, 15, 30, 60],
  "max_delay_seconds": 60,
  "max_attempts": 0,
  "jitter_ratio": 0.2,
  "poll_seconds": 5,
  "runtime_path": null
}
```

The schedule is applied only while the same thread keeps producing consecutive capacity failures. Jitter is symmetric at ±20% by default and each interval is capped at 60 seconds. `max_attempts: 0` means continue at the 60-second interval until the episode is cancelled or succeeds. The attempt budget is in memory and resets on restart.

```powershell
python .\src\codex_native_retry.py diagnose
Get-Content "$env:APPDATA\CodexNativeRetry\events.jsonl" -Tail 20
.\install.ps1 -StartWithWindows -Live  # optional live startup entry
.\start.ps1 -DryRun                    # one-shot passive startup
.\uninstall.ps1                         # remove that startup entry
```

Logs retain identifiers as short hashes and decision metadata, not conversation bodies or tool contents. Diagnose prints local installation paths; review them before sharing.

## Limitations

Run **one watcher per Codex home**. Deduplication currently protects a single process; there is no cross-process lock or persistent retry ledger. Thread checks and `turn/start` are separate RPCs, not an atomic compare-and-start operation. The tool avoids replaying earlier input, but cannot guarantee the model itself never repeats an action.

No production support for Desktop UI automation, remote hosts, arbitrary errors, or detached stdio sessions. Legacy Desktop research helpers remain in the source but are not part of the supported CLI workflow.

## Development

```powershell
python -m unittest discover -s tests -v
```

The public repository is a generated, one-way mirror. Maintainers develop in a private monorepo; issues and proposed patches are welcome here and are applied upstream before synchronization. Public commit messages and author metadata are normalized; private-to-public commit mappings remain private.

See [research](docs/native-retry-research.md), [protocol](docs/protocol-notes.md), [architecture](docs/architecture.md), [equivalence evidence](docs/native-retry-equivalence.md), and [safety boundaries](docs/safety-invariants.md).

[MIT License](LICENSE).
