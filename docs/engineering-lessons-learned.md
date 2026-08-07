# Engineering Lessons Learned

This is a living knowledge base for code reviewers and agents. Each entry describes a recurring bug pattern or subtle system behaviour that has appeared in past reviews. When reviewing a PR, check `docs/engineering-lessons-learned.md` for patterns that may be relevant to the diff.

If you find a new pattern during a review, add it here.

---

## PID Reuse Race

**Pattern:** A kill script saves a set of PIDs, sends SIGTERM, sleeps for a grace period, then sends SIGKILL to the original PID list.

**Why it matters:** The Linux kernel recycles PIDs aggressively. In the window between SIGTERM and SIGKILL, the original process may have exited and a completely unrelated process may have been assigned the same PID. The SIGKILL then kills the wrong process — silently, with no error.

**What to look for:** Any script that does roughly:
```bash
pids=$(pgrep ...)
kill $pids
sleep 5
kill -9 $pids  # danger: these PIDs may now belong to different processes
```

**Fix:** Track which PIDs actually received SIGTERM (i.e., which were alive at signal time). After the sleep, only SIGKILL processes that were in that set *and* are still alive. Check liveness before sending SIGKILL, or use process group signals with careful scoping.

---

## Missing `-a` Flag on `tmux list-panes`

**Pattern:** Code uses `tmux list-panes` or `tmux list-windows` without the `-a` flag to scan for running Claude sessions or other processes.

**Why it matters:** Without `-a`, tmux only lists panes in the *current session* (or the default session if run outside tmux). If Claude is running in a non-default tmux session — which is common in production — it will not appear in the output and will be misclassified as absent or as an orphan. This can trigger incorrect restarts or health-check failures.

**What to look for:**
```bash
tmux list-panes -F '...'        # wrong: only current session
tmux list-panes -a -F '...'    # correct: all sessions
```

**Fix:** Always use `-a` when the intent is to enumerate panes or windows across all tmux sessions.

---

## Execute Bit Drift

**Pattern:** A `git diff` shows a file mode change: `old mode 100644` → `new mode 100755` or vice versa.

**Why it matters:** Execute bit changes are invisible in most diff UIs — they show up in `git diff` output but not in GitHub's rendered diff by default. An unintentional `chmod +x` on a source file (especially a test file) can cause confusion and occasionally security surprises. Conversely, a script that needs to be executable (`#!/usr/bin/env bash`) but loses its execute bit will silently fail at runtime.

**What to look for:** In raw `git diff` output:
```
old mode 100644
new mode 100755
```
or the reverse.

**Questions to ask:**
- Does the file have a shebang line? If yes, `100755` is probably correct.
- Is this a test file run by pytest or another harness? Test files should not be executable (`100644`).
- Was this change intentional, or did it happen accidentally (e.g., via `cp` from a different filesystem)?

---

## PR Description Mismatch

**Pattern:** The PR title or description says one thing, but the diff does something different — or does less (or more) than described.

**Why it matters:** Reviewers and future readers rely on the PR description to understand intent. A mismatch creates two problems: (1) the reviewer may approve based on the description without scrutinising the actual change, and (2) the git history becomes misleading for future debugging.

**Common forms:**
- Description says "fixes X" but the diff only partially addresses X
- Description says "adds Y" but Y is not in the diff (it's in a separate PR)
- Description omits a significant side-effect of the change
- Title is generic ("fix bug") while the diff contains a meaningful, specific change worth naming

**What to do:** Flag mismatches explicitly in the review. Suggest a corrected description. Do not assume the diff is wrong — sometimes the description is the error.

---

## `RemainAfterExit=yes` in systemd + tmux

**Pattern:** A systemd service manages a tmux session and uses `RemainAfterExit=yes`. The `ExecStart` launches tmux, which detaches immediately. systemd marks the service active. Later, the tmux session dies.

**Why it matters:** `RemainAfterExit=yes` tells systemd: "consider this service active even after the process exits." Combined with tmux (which forks and exits the launcher), systemd will report the service as `active (exited)` indefinitely — even after the tmux session itself has been killed. `systemctl is-active` returns `active`, but nothing is actually running.

**What to look for:** Any health check or monitoring script that uses `systemctl is-active <service>` as a proxy for "the application is running" when that service uses `RemainAfterExit=yes` with tmux or any other daemonising process.

**Fix:** Check the actual running process, not the systemd unit status. For tmux, use `tmux has-session -t <session-name>` or `tmux list-sessions`. For other daemons, check the process directly (e.g., `pgrep`, `/proc/<pid>/status`).

---

## `rm -f` on a Socket File

**Pattern:** A restart or setup script does `rm -f /path/to/service.sock` before creating a new one.

**Why it matters:** `rm -f` unlinks the filesystem path unconditionally. If a server process is currently running and has the socket open, it keeps its open file descriptor — existing connected clients are unaffected. But new clients can no longer connect because the path is gone. The server does not receive any signal that this happened; it continues running normally while silently rejecting all new connections.

This is only safe to call during a controlled restart sequence where the old server process is torn down *before* the socket is unlinked, so there is no window in which the server is alive but unreachable.

**What to look for:** `rm -f *.sock` or `rm -f /run/*/socket` in scripts that do not also kill or stop the server in the same operation, or that kill the server *after* the unlink.

**Fix:** Stop the server first, then unlink the socket. Or use a pattern where the new server atomically replaces the socket (e.g., bind to a temp path and `mv` it into place).

---

## Dollar-Sign Mangling in Shell Strings (bcrypt hashes, Postgres passwords)

**Pattern:** A script or ad-hoc command passes a bcrypt hash (or any string containing `$`) to `psql` or another DB tool using double quotes or unquoted shell substitution.

**Why it matters:** bcrypt hashes start with `$2b$10$` and contain multiple `$` characters throughout. In double-quoted bash strings, `$` triggers variable expansion. `$2b` expands to the empty string (no such variable). `$10` expands to the 10th positional argument (also empty in most contexts). The result is a silently truncated and corrupted hash that is stored without error but will never match any password.

This is particularly insidious because: (1) the `psql` UPDATE command succeeds with exit code 0, (2) there is no warning in the output, and (3) the corruption is only discovered when a login attempt fails.

**What to look for:**
```bash
# Dangerous — $2b, $10, and other $ sequences will be expanded:
psql -c "UPDATE users SET password_hash = '$HASH' WHERE ..."
psql -c "UPDATE users SET password_hash = \"$HASH\" WHERE ..."
HASH='$2b$10$...'  # single-quoted assignment is fine
psql -c "UPDATE users SET password_hash = '$HASH' ..."  # but double-quoted interpolation is NOT
```

**Fix:** Use single quotes in the SQL literal, or pass the value via a heredoc with quoting:

```bash
# Safe option 1: single-quoted psql -c (no variable expansion inside SQL string)
HASH='$2b$10$abc123...'
psql -c "UPDATE users SET password_hash = '$HASH' WHERE email = 'user@example.com';"
# WARNING: this only works if HASH is assigned with single quotes AND the psql -c string
# uses double quotes for the outer shell string. Shell expands $HASH once, then psql
# receives the literal hash. But if HASH contains single quotes this breaks.

# Safe option 2: use psql with a heredoc (no risk of shell expansion of the hash value)
psql <<'EOF'
UPDATE users SET password_hash = '$2b$10$abc123...' WHERE email = 'user@example.com';
EOF

# Safe option 3: use psql -v to pass the value as a psql variable (safest for scripts)
HASH='$2b$10$abc123...'
psql -v hash="$HASH" -c "UPDATE users SET password_hash = :'hash' WHERE email = 'user@example.com';"

# Safe option 4: use node/python inside the container to generate AND apply the hash
# (avoids the shell layer entirely — preferred for one-time admin operations)
docker exec -it <container> node -e "
  const bcrypt = require('bcrypt');
  bcrypt.hash('mypassword', 10).then(h => console.log(h));
"
```

**Checklist for review:**
- Does any shell script pass a bcrypt hash, JWT secret, or other `$`-containing string to `psql -c "..."`?
- Is the outer shell string double-quoted? If so, flag it.
- Is the inner SQL string single-quoted? That helps but does not fully protect if the variable was expanded before substitution.

**Historical note:** This exact bug corrupted the Twenty CRM admin password hash during initial setup on 2026-03-23. The psql UPDATE succeeded silently; the corruption was discovered on first login attempt. Recovery required running `bcrypt.hash()` inside the Twenty Docker container and re-running the UPDATE.

---

## Dispatcher Detection: Why Two Files (startup-flag + session-id-marker)

**Pattern:** Dispatcher detection uses two separate files: a startup-flag written by the launcher, and a session-id-marker written by the SessionStart hook. Code may attempt to consolidate these into one file.

**Why it matters:** The dispatcher's Claude session ID (UUID) is not known at launch time — Claude generates it internally after startup. The launcher can only write a plain flag ("the next session is the dispatcher") but cannot predict what UUID Claude will assign. This makes a single pre-launch file with the session ID impossible.

The correct two-step sequence is unavoidable:
1. Launcher writes `startup-flag` (just a marker, no UUID) before exec
2. SessionStart hook reads the flag (detects it's the dispatcher), deletes it, then writes `session-id-marker` with the now-known session UUID
3. All subsequent hooks (PreToolUse, Stop) read `session-id-marker` — NOT the startup flag (which is already gone)

**What to look for:** Any attempt to:
- Write the session UUID before Claude starts (impossible — it doesn't exist yet)
- Use the startup flag in Stop hooks (it's consumed at SessionStart and will always be absent by Stop time)
- Consolidate detection into a single file without the two-step handoff

**Fix:** Preserve the two-step model. Use `is_dispatcher()` for SessionStart hooks (reads the startup flag only — the startup flag is still present at SessionStart time). Use `is_dispatcher_session()` for Stop and PreToolUse hooks (reads session-id-marker, with process-tree fallback for the early-boot window before any file is written).

**History:** Startup-flag model introduced in PR #1914 to replace fragile process-tree walking. Session-id-marker-at-Stop bug fixed in PR #1960 — DEAD state was never written because the startup flag was already consumed when Stop fired.

---

## Dispatcher-Exclusion Bug: The Dispatcher's Own Session Row Miscounted as a Dead/Pending Subagent

**Pattern:** The dispatcher registers itself as a row in `agent_sessions.db` (`agent_type='dispatcher'`) so hooks and reconciliation logic can identify the live dispatcher process. Unlike a real subagent, this row legitimately has `output_file=NULL`, no real task, and stays `status='running'` for the entire lifetime of the dispatcher process. Any code that scans `agent_sessions` — directly via SQL or indirectly via a `session_store.py` helper — to find dead, stale, completed, or pending *subagents* must explicitly exclude `agent_type='dispatcher'` rows. If it doesn't, the dispatcher's own row gets misclassified as a hung/dead/pending subagent.

**Why it matters:** This exact bug has been found and fixed **six separate times**, by different people, in different files, because nothing documented it as one recurring pattern:

1. **Issue #781** — first discovered: the dispatcher's `SessionStart` hook could mis-register it as a subagent, and the periodic reconciler loop would later mark it dead and enqueue a false `agent_failed` message. Fix applied only to the periodic loop (`reconcile_agent_sessions()` in `inbox_server.py`).
2. **PR #2099** — found the *same* missing exclusion in two more code paths that run at **restart time**, before the periodic loop's skip ever gets a chance to apply: `cleanup_stale_running_sessions()` (marks the dispatcher's always-`running`/`output_file=NULL` row dead on every proactive restart) and `get_unnotified_completed()` / `_startup_sweep()` (re-notifies unnotified dead/completed sessions, re-surfacing the dispatcher as a false `agent_failed: lobster-dispatcher` on every restart, roughly every 2 hours).
3. **PR #2103** — found a **fourth** occurrence: `scripts/periodic-self-check.sh` (cron, every 3 minutes) queries `agent_sessions.db` directly via raw `sqlite3`, bypassing `session_store.py` — and therefore bypassing every fix above. It counted `status IN ('running','starting')` rows as "pending agents" with no dispatcher exclusion, producing a permanent false-positive `[1 agents pending]` self-check firing every 3 minutes with zero real subagents active.
4. **PR #2152 / issue #2176** — found a **fifth** occurrence, introduced by PR #2152's new PID-ground-truth classification path: `scripts/agent-monitor.py`'s `load_running_agents()` never adopted the `utils.agent_types.DISPATCHER_EXCLUSION_SQL` fix that had already landed for `session_store.py` (occurrence #2) by the time #2152 was written — it was reinvented as a hand-copied `agent_id == "lobster-dispatcher"` string check in `mark_failed_all_ghosts()` instead of the query-boundary SQL filter, and that hand-copied check was itself incomplete: it only ever covered the `STALE_NO_FILE` list, not `GHOST_CONFIRMED`, and `send_alert()` had no guard on either. #2152's new PID-based path let the dispatcher's row (always `output_file=NULL`, previously routed only to `STALE_NO_FILE` under the legacy heuristic) land in `GHOST_CONFIRMED` instead whenever a restart raced the dispatcher's old, by-then-dead PID against its own re-registration — producing ~10 false `agent_failed` alerts against the live dispatcher in the 2 days after merge.
5. **Issue #2176 (same investigation)** — found a **sixth**, pre-existing occurrence unrelated to #2152 (predates it by ~1700 commits): `scripts/health-check-v3.sh`'s `count_active_subagents()` queried `SELECT COUNT(*) FROM agent_sessions WHERE status='running'` with no dispatcher exclusion at all. Since the dispatcher's own row is always `status='running'` for its entire lifetime, this count structurally always included at least 1 for the dispatcher itself — meaning `do_restart()`'s SUBAGENT GUARD (`if [[ "$active_subagents" -gt 0 ]]`) would defer *every* RED-state restart indefinitely whenever the dispatcher's row was present, regardless of whether any real subagent was actually running.

Each fix independently rediscovered the same root cause because there was no single place documenting "any code that lists/counts/reconciles agent-session rows needs this filter" — so nobody searching before writing new reconciliation code would have found it. Notably, occurrences #4 and #5 happened *after* the shared `utils.agent_types.DISPATCHER_EXCLUSION_SQL` / `scripts/lib/agent_sessions.sh` helper (see "Forward pointer" below) already existed — having the helper available did not, by itself, prevent new call sites from being written without it.

**What to look for:** Any new or modified code that:
- Queries `agent_sessions` (via `session_store.py` or a raw `sqlite3`/SQL call) with `WHERE status IN (...)` intended to find live, dead, completed, or stale *subagents*
- Iterates over `get_active_sessions()` / `get_unnotified_completed()` / any similar session list and reasons about "is this agent dead / stuck / pending"
- Counts or reconciles session rows for a health check, self-check, or notification path

...without filtering out `agent_type='dispatcher'` rows.

**Fix pattern:** Add the exclusion at the query or iteration boundary itself — not in some downstream consumer that might not be the only caller:
- SQL: `AND COALESCE(agent_type, '') != 'dispatcher'`
- Python iteration: `if (session.get("agent_type") or "") == "dispatcher": continue`

**All known affected call sites (as of this writing):**
1. `src/agents/session_store.py` — `cleanup_stale_running_sessions()` (SQL filter, via `DISPATCHER_EXCLUSION_SQL`)
2. `src/agents/session_store.py` — `get_unnotified_completed()` (SQL filter, via `DISPATCHER_EXCLUSION_SQL`)
3. `src/mcp/inbox_server.py` — `reconcile_agent_sessions()` periodic loop (Python skip)
4. `src/mcp/inbox_server.py` — `_startup_sweep()` (Python skip, mirrors #3)
5. `scripts/periodic-self-check.sh` — the `PENDING_COUNT` raw `sqlite3` query (SQL filter, via `scripts/lib/agent_sessions.sh`'s `DISPATCHER_EXCLUSION_SQL`)
6. `scripts/agent-monitor.py` — `load_running_agents()` (SQL filter at the query boundary, via `DISPATCHER_EXCLUSION_SQL`), plus a belt-and-suspenders `_is_dispatcher_agent()` guard applied directly in `mark_failed_all_ghosts()` (both the `confirmed` and `stale_no_file` lists) and `send_alert()` (issue #2176)
7. `scripts/health-check-v3.sh` — `count_active_subagents()` (SQL filter, via `scripts/lib/agent_sessions.sh`'s `DISPATCHER_EXCLUSION_SQL`; issue #2176)

**Forward pointer:** The shared, consolidated helper mentioned in earlier revisions of this entry now exists — `utils.agent_types.DISPATCHER_EXCLUSION_SQL` / `is_dispatcher_agent_type()` / `is_dispatcher_row()` for Python call sites, and `scripts/lib/agent_sessions.sh`'s `DISPATCHER_EXCLUSION_SQL` for shell call sites — and call sites #1, #2, #5, #6, and #7 above all use it. **Having the helper exist was not sufficient to prevent occurrences #4 and #5 (2026-08)**: both were new code added after the helper already existed, and neither adopted it until this fix. The helper removes the *cost* of doing the right thing but does not by itself catch a new call site that skips it. A structural fix that would: a repo-wide check (grep-based CI lint, or a single pytest that scans all `FROM agent_sessions` / `agent_sessions.db` query sites in the repo and asserts each either uses the shared exclusion helper or is on an explicit documented allowlist) — is still not implemented as of this writing, despite being flagged as needed after occurrence #4. Until it lands, any new agent-session query must apply the filter/skip pattern manually and should reference this entry.

**History:** Issue #781 (initial partial fix — periodic loop only) → PR #2099 (extended to `cleanup_stale_running_sessions()`, `get_unnotified_completed()`, and `_startup_sweep()`) → PR #2103 (extended to `periodic-self-check.sh`'s raw SQL query, which bypasses `session_store.py` entirely) → PR #2152 introduced occurrence #5 (`scripts/agent-monitor.py`'s new PID-ground-truth path), fixed together with the pre-existing, independent occurrence #6 (`scripts/health-check-v3.sh`'s `count_active_subagents()`) via issue #2176.
