# Design: Self-Routed LLM Checks (No New Anthropic API Calls From Hooks)

Status: **IMPLEMENTED.** The recommendation in §5/§8 was built and merged as
`hooks/agent-git-push-guard.py` in [PR #2171](https://github.com/SiderealPress/lobster/pull/2171),
closing [BIS-750](https://linear.app/fully-parsed/issue/BIS-750). Committed
here after the fact as a historical record of the design rationale (the
Shape A/B distinction in §2, the alternatives considered and rejected in §6,
and the cost/tradeoff summary in §7) — none of that reasoning is captured in
the PR description itself. The body below is otherwise unchanged from the
original draft and reads in the future tense/design-proposal voice.

## 1. The ask

aeschylus, 2026-08-05: "Calling the anthropic API costs way too much money. Zoom out and
design a way for the Lobster instance to call itself in such situations." Concrete
example: the semantic PII/secret detection use case from BIS-755 (the pre-push PII
scanner built in PR #2156/#2169, found uninstalled/inert, closed via BIS-756 with a
decision *not* to install it — the existing regex-based `.githooks/pre-push` already
covers this without API cost).

Follow-up refinement, same day: "Use fable and opus to figure out how to add the
feedback message to the hook so any agent would just naturally receive it in the
course of its activities and have to respond to it." This sharpens the ask
considerably — it's not "queue a message and poll for a reply," it's "use the
tool-call-blocking mechanism Claude Code already has, so the check rides on a turn
the agent is already having." That is a materially cheaper and simpler mechanism
than anything queue-based, and this doc leads with it.

## 2. What "call itself" can concretely mean — two different caller shapes

The phrase "call itself" collapses two very different situations that need
different answers. Getting this distinction right is the core of the design.

### Shape A: A human is running `git push` at a terminal

There is no Claude Code turn in progress. Nothing is "calling itself" here — the
process tree is `bash → git → .githooks/pre-push`, full stop. The pre-push hook can
prompt on `/dev/tty` and block, or run in CI mode and just warn. This is exactly
what `.githooks/pre-push` already does today, entirely with local regex, zero API
calls. **This case was already resolved** by BIS-756: don't add API calls here, the
regex hook is sufficient. Nothing in this design changes that.

### Shape B: An agent (dispatcher or subagent) is running `git push` via its Bash tool

This is the case aeschylus's follow-up is actually about. When a Claude Code agent
(e.g. a `functional-engineer` subagent finishing up a PR) calls the `Bash` tool
with a command containing `git push`, Claude Code itself sits in the loop as the
tool dispatcher. A **PreToolUse hook** fires before the command executes, reads
`tool_input` (including the command string) from stdin, and can:

- exit 0 → allow the tool call to proceed normally
- exit 2 → **block the tool call**, and Claude Code shows the hook's stderr output
  to the calling model **as part of its own next turn in the same session**

That second path is the "call itself" mechanism. The agent that ran `git push`
is the same agent that now sees "possible PII detected — assess before I let this
through" and has to react to it, in its own ongomg turn. No new Anthropic session
is created. No inbox message is written. No polling. This is not a hypothetical —
**it's exactly what this codebase already does** in several places:

- `hooks/link-checker.py` — blocks `send_reply` (exit 2) when a message claims
  completed work but has no clickable link, injecting a stderr message the
  dispatcher must act on before it can send.
- `hooks/require-write-result.py` — blocks a subagent's `Stop` (exit 2) until it
  calls `write_result`, with a bounded retry counter (`MAX_HOOK_FIRES`) and a
  fallback synthetic result after N fires.
- `hooks/require-background-agent.py` — blocks `Agent`/`Task` calls (exit 2) that
  lack background intent.
- `hooks/secret-scanner.py` — **already fires on `Bash` commands containing
  `gh issue`/`gh pr`** and on `mcp__github__*` writes, scanning for known secret
  values. It is currently warn-only (exit 0 always) by explicit design — see
  issue #582, which shipped it as v1 with block mode called out as deliberate
  future work once pattern coverage is trusted.

So the mechanism aeschylus is asking for is not new to this system. It's the
established PreToolUse-block-and-inject pattern, applied to one more tool call
shape (`Bash` commands containing `git push`) that doesn't have it yet.

## 3. Why this specific gap exists today

`.githooks/pre-push` already tries to block on findings — but only when stdin is a
TTY:

```bash
IS_TTY=1
[ ! -t 0 ] && IS_TTY=0
...
# CI / non-interactive mode: print findings but do not block
if [ "$IS_TTY" -eq 0 ]; then
    warn "Non-interactive mode (CI). Running scan but will not block push."
    ...
    exit 0
fi
```

When a Claude Code agent runs `git push` through the `Bash` tool, stdin is not a
TTY. So today, an agent-initiated push with real findings gets warned in the
Bash output and **the push proceeds anyway** — the exact interactive
confirm-and-abort flow a human gets is silently skipped for agents. This is a real,
currently-live gap, independent of whether aeschylus asked about it: agent-driven
pushes get weaker protection than human-driven ones, for a reason (no TTY to
prompt) that a Claude Code PreToolUse hook does not share, because it talks to the
agent's own reasoning loop instead of a human at a keyboard.

## 4. Disentangling aeschylus's actual concern(s)

All three of the candidate concerns are real and this design addresses all of them:

- **(a) Dollar cost of extra API calls** — a hook that shells out to the Anthropic
  API is a new, separately-billed inference session (cold context, full system
  prompt, its own line item) on every single git push, most of which have nothing
  wrong with them. The design in §5 makes the marginal cost on a clean push
  **exactly zero** — the hook is pure local regex, same as today. Cost is only
  incurred on an actual finding, and even then it's a few hundred extra tokens
  inside a turn the agent (subagent doing PR work) was already going to take —
  not a new session.
- **(b) Latency / blocking a human during `git push`** — irrelevant to Shape B by
  construction: the "human sitting in front of a blocking prompt" scenario is
  Shape A, which is unchanged (still the existing interactive `.githooks/pre-push`
  confirm). Shape B's caller is an unattended background subagent (e.g.
  `functional-engineer` running a PR workflow) — an extra agent turn to resolve a
  flagged finding costs seconds, not human patience.
- **(c) Architectural cleanliness (hooks should be simple/local; LLM smarts belong
  in the orchestrated agent layer)** — this is exactly what §5 delivers: the hook
  itself never gets smarter than regex. All actual judgment ("is this really PII
  or a false positive") is deferred to the calling agent's own already-running
  reasoning, not embedded in the hook.

## 5. Recommended design (narrow, scoped to the actual use case)

**Do not build a generic "hook calls the dispatcher" framework.** There is no
second real use case in front of us today that needs it (see §6 for why the
inbox/polling idea from the first pass at this design is explicitly *not* being
built now). Build the smallest thing that closes the actual gap in §3.

### New hook: `hooks/agent-git-push-guard.py`

- **Trigger:** `PreToolUse`, matcher `Bash`, firing only when the command string
  matches a `git push` shape (reuse the detection style already used in
  `secret-scanner.py`'s `_BASH_GH_WRITE_RE` — a compiled regex over
  `tool_input["command"]`, no new dependency).
- **Scope guard:** only acts when the target remote resolves to the public
  `SiderealPress/lobster` repo (same repo-scoping intent as the banner in
  `.githooks/pre-push`) — skip everything else silently (exit 0).
- **Logic:** port (not reinvent) the same pattern tables already defined in
  `.githooks/pre-push` (`INSTANCE_PATTERNS`, `PII_PATTERNS`, `SECURITY_PATTERNS`,
  `NAME_PATTERNS`) into a small shared Python module both the git hook and this
  new Claude Code hook can import, OR simply shell out to
  `.githooks/pre-push`'s own `scan_diff` logic in a mode that returns findings
  as data instead of prompting. Either way: **one source of truth for the
  patterns**, not a second regex set to keep in sync.
- **On a clean diff:** exit 0. Silent, free, identical to today.
- **On findings:** exit 2, stderr message listing the findings plus an explicit
  instruction: *"Possible PII/secrets detected in this push — [list]. Assess
  whether each is a real finding or a false positive. If real, fix and retry the
  push. If a false positive, say why in your next message and retry — this will
  be logged."* This is delivered into the calling agent's own turn, per §2.
- **Repeat-block bound:** reuse the exact retry-counter convention from
  `require-write-result.py` (`MAX_HOOK_FIRES`, a small state file keyed by
  session, incrementing per re-fire). Cap at ~2–3 retries per push attempt, then
  fail open with a clearly logged warning (matching the existing
  `.githooks/pre-push` policy of never blocking indefinitely) rather than
  wedging a subagent forever on a false-positive loop.
- **No Anthropic API call anywhere in this file.** The hook is exactly as "dumb"
  as `secret-scanner.py` and `.githooks/pre-push` are today — it only decides
  block-vs-allow from regex. The semantic judgment step is the agent's own next
  turn, already running on whatever model tier that agent normally uses
  (`functional-engineer` is Opus-tier per the model table in CLAUDE.md; the
  dispatcher itself rarely runs `git push` directly per the worktree convention,
  but if it did, judgment would be Sonnet-tier — still adequate for a
  confirm-or-fix step, not a substitute for a human reviewing the actual PR).

### Register it

Same install-time wiring as every other hook here: add to `PreToolUse` in
`~/lobster/.claude/settings.json` (and the install/upgrade scripts that seed new
installs — `install.sh`, `scripts/upgrade.sh`, matching the precedent set by
issue #582 for `secret-scanner.py`), matcher `Bash`.

## 6. What I looked at and decided *not* to recommend

**Message-queue / inbox round-trip** (a hook writes a message into
`~/messages/inbox/` and blocks/polls for a dispatcher or subagent response) — this
was my first-pass answer before aeschylus's follow-up narrowed the ask, and it's worth
recording why it's the wrong tool for Shape B specifically, while still being the
right *fallback* for a case this design doesn't need to solve today:

- It only makes sense when there is **no existing agent turn to inject into** —
  e.g., a bare cron script or a genuinely non-agent process that wants an LLM
  opinion. That's not `git push` from an agent's Bash tool call (Shape B has an
  agent turn right there) and it's not a human at a terminal (Shape A already has
  its answer and doesn't want LLM latency in the loop at all).
- Latency is real and unavoidable: writing to `~/messages/inbox/` and waiting for
  `wait_for_messages()` to notice it, dispatch to a subagent, and write a result
  back is a multi-second-to-minutes round trip through the normal dispatcher
  queue — fine for async work, wrong for anything that wants to feel like part of
  the same operation.
- It would still hit the same 7-second-rule / background-subagent machinery this
  system already has (`Task`/`Agent` spawning, `write_result`, `session_start`) —
  so it's not a new primitive, just a slower, more indirect way to get an LLM
  opinion than "the agent already running the tool call reads the hook's stderr
  and responds." If a future use case shows up where the caller truly has no
  agent turn (e.g. a pure `cron`-triggered non-Claude script wants semantic
  judgment on something), this pattern is the right one to build **then**,
  reusing `write_result`/`wait_for_messages` as-is rather than inventing a new
  channel — but there is no such use case in front of us right now, so building
  it speculatively would be exactly the kind of over-built generic framework the
  task brief warned against.

**A fresh Anthropic API call from inside any hook** — explicitly rejected per
aeschylus's original directive (this is the thing BIS-756 already decided against for
the PII scanner). Nothing in this design revisits that; §5 has zero API calls in
the hook itself under any code path.

**Converting `secret-scanner.py` to block mode in general** — out of scope here.
It already warn-only scans `send_reply` and GitHub writes for *known* secret
values from config (issue #582 flagged block mode as deliberate future work, not
blocked on this design). The new hook in §5 is a different, narrower thing: a
diff-level PII/instance-data scan specifically on `git push`, ported from the
patterns already in `.githooks/pre-push`. Worth revisiting `secret-scanner.py`
block mode as a *separate*, later piece of work using the same retry-counter
convention — not folding it into this change.

## 7. Cost/tradeoff summary

| | Human at terminal (`git push`) | Agent via Bash tool (`git push`) |
|---|---|---|
| Mechanism | Existing `.githooks/pre-push`, interactive TTY prompt | New `agent-git-push-guard.py` PreToolUse hook, exit-2 block |
| API calls added | Zero (unchanged) | Zero in the hook; judgment is the agent's own next turn (tokens, not a new session) |
| Latency added on clean diff | Zero | Zero |
| Latency added on a finding | N/A (human reads prompt, decides) | Seconds (one extra agent turn) — acceptable, caller is an unattended subagent, not a human waiting on `git push` |
| Blocking behavior today | Blocks (has a TTY) | **Does not block today** (no TTY → CI-mode warn-only) — this is the gap being closed |
| Semantic ("is this really PII") judgment | Human | The calling agent's own reasoning, at whatever model tier it already runs |

## 8. Recommendation

Build only §5: a single new PreToolUse hook, `hooks/agent-git-push-guard.py`,
matcher `Bash`, firing on `git push`-shaped commands targeting the public repo,
reusing `.githooks/pre-push`'s existing pattern tables (ported to Python, one
source of truth), blocking via exit 2 with the existing `require-write-result.py`
retry-counter convention, and no API calls anywhere in the hook. This closes a
real, currently-live gap (agent-initiated pushes silently skip the block-on-
finding behavior that human-initiated pushes already get) using a mechanism the
codebase already has multiple working examples of, at zero marginal API cost on
the common case and no new architecture.

Do not build: a generic hook-to-dispatcher message bus, or any hook that calls
the Anthropic API directly. Neither is needed for the use case in front of us.

This is a design doc only. If aeschylus wants to proceed, the natural next step is a
GitHub issue under the BIS-755 remediation umbrella (sibling to BIS-756/757/758)
scoped exactly to §5, handed to `functional-engineer` with the same
proof-of-work bar (falsifiable automated test + manual demonstration) used on the
rest of that remediation series.
