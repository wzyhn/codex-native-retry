# Safety boundaries

Implemented boundaries:

- Never submit a natural-language continuation or resend an original prompt.
- Never edit conversation files or create a replacement thread for recovery.
- Only terminal capacity failures are eligible; explicit `willRetry:true` is ignored.
- Use the existing shared daemon and same thread; do not attach to private stdio sessions.
- Check non-active loaded status and the exact latest failed capacity turn before sending.
- Do not supply optional settings overrides.
- Cancel obsolete episodes on new turns or user activity.
- Stop after an ambiguous send result.
- Persist metadata only: hashed IDs, classification, attempts, and decisions.

Current limitations:

- Deduplication and retry budgets are in memory, within one process.
- The server does not expose an atomic failed-turn compare-and-start operation here.
- Startup scans and remote metadata checks do not prove absence of every external race.
- The model can still repeat an action during native continuation; the companion makes no absolute side-effect guarantee.
- Legacy UI helpers are outside the supported CLI workflow.

Use dry-run to assess compatibility before live operation. See [equivalence evidence](native-retry-equivalence.md).
