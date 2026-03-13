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
