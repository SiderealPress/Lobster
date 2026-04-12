## Hibernation — DEPRECATED

Dispatcher hibernation has been removed. The `hibernate_on_timeout=True` flag and the
`mode=hibernate` state are no longer used.

**Do not call `wait_for_messages` with `hibernate_on_timeout=True`.** The dispatcher runs
a permanent loop and never exits cleanly. Recovery from frozen `wait_for_messages` calls
is handled by the WFM watchdog (PR #1446).

If you see `mode=hibernate` in `lobster-state.json`, it was written by an older version
of the code. The health check no longer has a `hibernate` branch — the state will be
treated as `unknown` and full active-mode checks will apply.

For background: issues #1442 and #1448, PRs #1446 and #1447.
