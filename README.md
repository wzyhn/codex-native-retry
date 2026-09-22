# Codex Native Retry

[简体中文](README.zh-CN.md)

An **unofficial, experimental Windows companion for Codex CLI**. Recover terminal model-capacity failures through the existing thread's native empty-input continuation:

```json
{"method":"turn/start","params":{"threadId":"<existing-thread>","input":[]}}
```

No synthetic “continue” message, prompt replay, new conversation, or model/permission overrides. This project is not affiliated with or endorsed by OpenAI.

## Requirements

- Windows 10/11, Python 3.10+ (standard library only).
- A compatible Codex runtime and a CLI session connected to a **shared local app-server**.
- Tested with runtime `0.154.0-alpha.6.2` and CLI `0.155.1`. Protocols change; other versions require verification.

A normal CLI session using its own stdio server cannot be attached to. Starting this watcher alone does not make an existing stdio session recoverable.

## Quick start

Clone this repository and open PowerShell in its root:

```powershell
git clone https://github.com/wzyhn/codex-native-retry.git
cd codex-native-retry
python .\src\codex_native_retry.py diagnose
python .\src\codex_native_retry.py watch --dry-run
```

Dry-run observes errors and records `would_retry` without starting a turn.

For live recovery, keep these running in separate terminals:

```powershell
# Terminal 1: local shared server (keep this terminal open)
codex app-server --listen unix://

# Terminal 2: choose and resume your existing conversation
codex --remote unix:// resume

# Terminal 3: from this repository
python .\src\codex_native_retry.py watch --live --retry-mode native_cli
```

Close the old stdio CLI before opening the same conversation through `--remote`. A managed standalone installation can alternatively use `codex app-server daemon start`.

If runtime discovery fails, pass the actual `codex.exe` path with `--runtime` to both `diagnose` and `watch`, or set `runtime_path` in the configuration. Discovery currently checks the Desktop runtime cache; an explicit CLI executable also works. If `codex app-server` is unavailable in your shell, use that executable to run the server.

## When it acts

Only a terminal `serverOverloaded` / `server_overloaded` failure is eligible. The exact capacity message is a compatibility fallback only when no structured type is present.

- `willRetry=true`: Codex handles its own retry.
- Watcher startup also considers existing terminal capacity errors.
- Immediately before sending, the daemon must show `idle` or `systemError` and the **same latest failed turn**, still classified as capacity.
- An active/unloaded thread, new turn, manual retry, or new user message blocks the old episode.
- A transport timeout after sending stops the episode; an ambiguous request is never blindly retransmitted.

The continuation creates a new native turn inside the same thread. `accepted` means the server accepted the request; it does not guarantee model capacity is available or the task finishes.

## Settings and diagnostics

`install.ps1` creates `%APPDATA%\CodexNativeRetry\config.json`. These are the defaults:

```json
{
  "enabled": true,
  "dry_run": true,
  "retry_mode": "dry_run",
  "initial_delay_seconds": 5,
  "max_delay_seconds": 60,
  "max_attempts": 5,
  "jitter_ratio": 0.2,
  "poll_seconds": 5,
  "runtime_path": null
}
```

Backoff doubles from 5 seconds, with jitter, capped at 60 seconds. Recognized retry-after metadata may extend the wait. `max_attempts: 0` opts into unlimited attempts. The attempt budget is currently in memory and resets on restart.

```powershell
python .\src\codex_native_retry.py diagnose
Get-Content "$env:APPDATA\CodexNativeRetry\events.jsonl" -Tail 20
.\install.ps1 -StartWithWindows  # optional startup entry, dry-run only
.\uninstall.ps1                # remove that startup entry
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
