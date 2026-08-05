# Dependency Pinning Policy

**Effective 2026-08-04.** Prompted by a Shai-Hulud-style npm supply-chain
worm investigation (system found clean, but the risk of a compromised
package silently landing via a `^`/`~`/unbounded range was judged real
enough to close off structurally, not just by vigilance).

## Policy

Every dependency manifest in this system is pinned to an **exact** version.
No `^`, `~`, `~=`, `>=`, `<=`, `!=`, `>`, `<`, `*`, or `"latest"` ranges.
`npm install` / `pip install` / `uv sync` / equivalent must never be able to
silently resolve a dependency to a newer version than what a human
explicitly reviewed and pinned.

Bumping a pinned version is still fully supported — it just has to be a
deliberate edit to the manifest (changing `=="1.2.3"` to `=="1.3.0"`), not
something a package manager decides for you.

## Enforcement

- `hooks/pin-dependencies-guard.py` — Claude Code PreToolUse hook. Blocks
  Edit/Write/NotebookEdit calls that introduce an unpinned range into a
  manifest, and blocks Bash package-manager invocations that could resolve
  to "whatever is newest right now" (bare `npm install <pkg>`, `npm
  update`, `pip install <pkg>` without `==`, `pip install --upgrade`, `uv
  add <pkg>` without `==`, `uv sync`/`uv lock` with `--upgrade`). See the
  module docstring for the full, precise list of what does and doesn't
  trigger a block.
- `hooks/pre-commit` — a defense-in-depth backstop at `git commit` time,
  scanning staged manifest content with the same detection logic (via
  `pin-dependencies-guard.py --scan-file`). This catches edits made outside
  Claude Code's own tools (a plain editor, `git apply`, an ad hoc script).
- Escape hatch: `LOBSTER_ALLOW_DEPENDENCY_CHANGE=true` in the environment
  bypasses both checks for one deliberate, reviewed bump. This is a
  dedicated variable, separate from `LOBSTER_DEBUG` (which is already set
  to `true` persistently in this system's `settings.json` for unrelated
  reasons — reusing it here would make the hook a permanent no-op).

## Manifests covered and current pin status

| Manifest | Status as of audit | Notes |
|---|---|---|
| `pyproject.toml` (root) | Pinned | All `>=` ranges replaced with the version already resolved in `uv.lock`. `hatchling==1.31.0` verified from local `uv` cache (the version actually used to build). |
| `uv.lock` (root) | Regenerated | `uv lock` re-run after pinning; only removes "this is the floor of a range" metadata — resolved versions unchanged. |
| `connectors/whatsapp/package.json` | Pinned | Removed `^` from all three deps (already at latest-compatible: chokidar 3.6.0, qrcode 1.5.4, whatsapp-web.js 1.26.0). |
| `connectors/whatsapp/package-lock.json` | **Not committed — see Known Gaps** | This directory's own `.gitignore` blanket-excludes lock files. |
| `lobster-shop/multiplayer-telegram-bot/pyproject.toml` | Pinned | `setuptools==83.0.0`, `pytest==9.0.2` (both dev-dependency declarations aligned). Verified via `uv pip install --dry-run` (no local install existed). |
| `lobster-shop/multiplayer-telegram-bot/uv.lock` | Regenerated | `uv lock` re-run; no version changes. |
| `lobster-shop/obsidian-km/requirements.txt` | Pinned | `mcp==1.26.0` and `python-dotenv==1.2.1` aligned to the root project's pins (this MCP server runs standalone but sharing a pin avoids two different resolved versions of the same package existing in the system for no reason). `python-frontmatter==1.3.0` verified via dry-run resolve (no existing anchor). |
| `tests/requirements-test.txt` | Pinned | Packages that also appear in root `pyproject.toml` are pinned to the SAME version as root (see "Why not just pin to latest" below). Test-only packages (`docker`, `aioresponses`, `faker`, `aiofiles`) pinned to a dry-run resolve; `responses==0.26.0` and `freezegun==1.5.5` pinned to the versions actually found installed on this host. |
| `lobster-shop/camofox-browser/server/package.json` | **Not touched — see Known Gaps** | External vendored product, out of scope. |

## Why not just pin test deps to "whatever's latest today"

Before pinning, `tests/requirements-test.txt` had `mcp>=1.0.0`,
`pytest>=8.0.0`, `python-telegram-bot>=20.7`, all unbounded. A dry-run
resolve of that file as it stood (`uv pip install --dry-run -r
tests/requirements-test.txt` against a scratch venv) came back with
`mcp==2.0.0` and `python-telegram-bot==22.8` — a full major version ahead
of the versions actually pinned in the root project (`mcp==1.26.0`,
`python-telegram-bot==22.6`). That is precisely the failure mode this
policy exists to close: a test run today would have silently exercised
different dependency versions than the running system. The fix applied
here was to pin every package that's shared with the root project to the
SAME already-pinned version, not to whatever a fresh resolve returns.

## Known gaps / things flagged rather than silently worked around

1. **`connectors/whatsapp/package-lock.json` does not exist and is not
   committed.** `connectors/whatsapp/.gitignore` has a blanket
   `# Lock files (generated)` exclusion covering `package-lock.json` and
   `yarn.lock`. Direct dependencies in `package.json` are now pinned
   exactly, but without a committed lockfile, *transitive* dependencies of
   `whatsapp-web.js` (which pulls in `puppeteer`, which downloads a
   platform-specific Chromium binary) are still free to float on every
   fresh `npm install`. Generating and committing a lockfile there conflicts
   with an existing, deliberate repo convention — this needs an explicit
   decision from the system owner rather than being overridden unilaterally.

2. **`lobster-shop/camofox-browser/server` was not modified.** It is
   vendored into this tree as a nested git checkout (a "gitlink" — `git
   diff` shows it as a `160000` mode entry) but has no corresponding
   `.gitmodules` registration, and its `origin` remote points at a separate
   product repo (`jo-inc/camofox-browser`, maintained by Jo Inc, not part of
   the Lobster system). Its `package.json` still uses `^` ranges for
   `express`, `playwright-core`, `prom-client`, `swagger-jsdoc`,
   `camoufox-js`, and its devDependencies. It does carry its own
   `package-lock.json`, so transitive resolution is currently frozen at
   whatever that lockfile last resolved to — but the `^` ranges mean a
   plain `npm update` inside that nested repo would still float. Editing a
   foreign repo's own manifest/history from inside this task felt out of
   scope and risky without the system owner or Jo Inc's maintainers reviewing it
   separately; flagging here rather than silently leaving it un-mentioned.

3. **Scope of this pass was the `~/lobster` system repo itself**, not every
   directory under `~/lobster-workspace/projects/`. Everything else living
   in `lobster-workspace/projects/` that is a *worktree of this same repo*
   (`feature-issue-*`, `fix-issue-*`, `bis-74*`, etc.) is automatically
   covered once this branch merges to `main`, since they share the same
   git history. But `lobster-workspace/projects/` also holds a number of
   entirely separate, unrelated products with their own git repos and
   dependency surfaces (Eloso Bisque, Paperclip, MyOwnLobster, vault-api /
   vault-ws, networkmaps, bisque-booking, bottleneck-beta, lobster-watcher)
   — these were surveyed at a high level but deliberately not modified.
   "The Lobster system" was read as this repo; pinning across a dozen-plus
   unrelated client codebases in the same pass, each with its own release
   process, felt like scope creep with real risk of breaking unrelated
   builds. If the system owner wants the policy extended to those repos, that should be
   a separate, explicit follow-up per repo.

## Pre-existing test suite status (not caused by this PR)

`uv run pytest tests/unit/ --tb=no -q` currently reports **16 pre-existing
failures** (not 8 — an earlier count in this PR undercounted them), spread
across **7 files**, none related to dependency pinning:

- `tests/unit/test_bot/test_slack_router_config.py` — 1
- `tests/unit/test_hooks/test_context_monitor.py` — 1
- `tests/unit/test_hooks/test_on_compact_write_claude_session_id.py` — 7
- `tests/unit/test_mcp_server/test_push_calendar_token_endpoint.py` — 2
- `tests/unit/test_mcp_server/test_push_gmail_token_endpoint.py` — 2
- `tests/unit/test_mcp_server/test_push_workspace_token_endpoint.py` — 2
- `tests/unit/test_script_inbox_sources.py` — 1

Confirmed identical (same 16, same files) on both `main` and this branch —
this PR does not introduce, fix, or otherwise touch any of them. Scope is
`pytest tests/unit/` per this repo's own Makefile; a literal unscoped `uv
run pytest --tb=no -q` from repo root additionally produces 43 collection
errors on both branches, a pre-existing monorepo/subproject
dependency-isolation issue also unrelated to this PR.
