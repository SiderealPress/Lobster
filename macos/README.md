# Lobster Local Sync -- macOS Integration

Continuously backs up your local git working trees (including uncommitted and untracked files) to `lobster-sync` branches on GitHub, with zero disruption to your workflow.

## How It Works

```
┌──────────────────────────────────────────────────────┐
│                     Your Mac                          │
│                                                       │
│  lobster-sync daemon (launchd)                       │
│  ├── Every 5 min (configurable):                     │
│  │   ├── For each registered repo:                   │
│  │   │   ├── git write-tree (temp index)             │
│  │   │   ├── git commit-tree (no branch switch)      │
│  │   │   └── git push origin lobster-sync --force    │
│  │   └── Log results                                 │
│  └── Runs continuously via launchd KeepAlive         │
│                                                       │
│  lobster-sync CLI                                    │
│  ├── add/remove repos                                │
│  ├── check status                                    │
│  ├── trigger immediate sync                          │
│  └── start/stop service                              │
└──────────────────────────────────────────────────────┘
```

The sync uses git plumbing commands (`write-tree`, `commit-tree`, `update-ref`) with a temporary index file. Your working directory, staged changes, and current branch are never touched.

## Prerequisites

- **macOS** (launchd-based service management)
- **git** (included with Xcode Command Line Tools)
- **jq** (for JSON config handling): `brew install jq`
- A **GitHub personal access token** with `repo` scope

## Quick Start

```bash
# Clone the Lobster repo (if you haven't already)
git clone https://github.com/SiderealPress/Lobster.git
cd Lobster

# Run the installer
./macos/install-lobster-local.sh

# Register repos for sync
lobster-sync add ~/projects/my-app
lobster-sync add ~/projects/another-project

# Check everything is working
lobster-sync status
```

## Installation

The installer (`install-lobster-local.sh`) performs these steps:

1. Creates `~/.lobster/` directory structure (`bin/`, `logs/`, `config/`)
2. Copies sync scripts to `~/.lobster/bin/`
3. Generates default `sync-config.json`
4. Installs launchd plist to `~/Library/LaunchAgents/`
5. Walks you through GitHub token setup (with Keychain option)
6. Optionally configures Claude Code MCP settings

The installer is **idempotent** -- running it again updates scripts and the plist while preserving your config and registered repos.

### Options

```bash
./macos/install-lobster-local.sh              # Interactive install
./macos/install-lobster-local.sh --no-start   # Install without starting service
```

## CLI Reference

### `lobster-sync add <path>`

Register a git repository for continuous sync.

```bash
lobster-sync add ~/projects/my-app
lobster-sync add ~/projects/govscan
```

The repo is added to `~/.lobster/sync-config.json`. If the repo was previously removed, it is re-enabled.

### `lobster-sync remove <path>`

Unregister a repository. The repo itself and its sync branch are not deleted.

```bash
lobster-sync remove ~/projects/old-project
```

### `lobster-sync status`

Show the sync status for all registered repos and the daemon service.

```bash
$ lobster-sync status
--- Service ---
  launchd:  loaded
  daemon:   running (PID 12345)
  interval: 300s

--- Repos ---
  my-app               ~/projects/my-app              last sync: 2m ago     ok
  govscan              ~/projects/govscan              last sync: 2m ago     ok
  old-project          ~/projects/old-project          last sync: 15m ago    stale
```

Status indicators:
- **ok** (green) -- synced within 2x the interval
- **stale** (yellow) -- last sync was longer ago than expected
- **never synced** -- no sync branch exists yet
- **disabled** -- repo is registered but disabled
- **missing** -- repo path no longer exists

### `lobster-sync now`

Trigger an immediate sync of all registered repos (runs one cycle and exits).

```bash
lobster-sync now
```

### `lobster-sync log`

Tail the sync daemon log.

```bash
lobster-sync log
```

Press `Ctrl-C` to stop tailing.

### `lobster-sync start`

Load the launchd service (starts the daemon).

```bash
lobster-sync start
```

### `lobster-sync stop`

Unload the launchd service (stops the daemon).

```bash
lobster-sync stop
```

## Configuration

### `~/.lobster/sync-config.json`

```json
{
  "sync_interval_seconds": 300,
  "sync_branch": "lobster-sync",
  "repos": [
    {
      "path": "/Users/drew/projects/my-app",
      "remote": "origin",
      "enabled": true
    }
  ]
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `sync_interval_seconds` | `300` | Seconds between sync cycles |
| `sync_branch` | `lobster-sync` | Branch name for sync commits |
| `repos[].path` | -- | Absolute path to the git repo |
| `repos[].remote` | `origin` | Git remote to push to |
| `repos[].enabled` | `true` | Set to `false` to skip this repo |

You can edit this file directly or use the CLI (`add`/`remove`) to manage repos.

### Per-Repo Exclusions

Create a `.lobster-sync-exclude` file in any repo to exclude files from sync (uses `.gitignore` syntax):

```gitignore
# .lobster-sync-exclude
node_modules/
.env.local
build/
*.sqlite
```

## GitHub Token Setup

The sync daemon needs a GitHub token with `repo` scope to push sync branches.

### macOS Keychain (Recommended)

The installer offers to store the token in macOS Keychain. This is the most secure option -- the token is encrypted at rest and never stored in a plaintext file.

**Manual Keychain setup:**

```bash
# Store token
security add-generic-password \
  -s lobster-sync \
  -a github-token \
  -w "ghp_your_token_here"

# Verify it works
security find-generic-password \
  -s lobster-sync \
  -a github-token \
  -w
```

### Fallback: `.env` File

If Keychain is not used, the token is read from `~/.lobster/.env`:

```bash
GITHUB_TOKEN=ghp_your_token_here
```

The CLI checks Keychain first, then falls back to the `.env` file.

## Logs

| File | Description |
|------|-------------|
| `~/.lobster/logs/sync.log` | Daemon log (sync cycle results) |
| `~/.lobster/logs/sync-stdout.log` | launchd stdout capture |
| `~/.lobster/logs/sync-stderr.log` | launchd stderr capture |

## Uninstalling

```bash
./macos/uninstall-lobster-local.sh           # Interactive
./macos/uninstall-lobster-local.sh --full    # Remove everything
```

The uninstaller:
1. Stops and unloads the launchd service
2. Removes the plist from `~/Library/LaunchAgents/`
3. Removes the CLI symlink from `/usr/local/bin/`
4. Optionally removes the Keychain entry
5. Optionally removes `~/.lobster/` (config, logs, scripts)

**Not removed:** Git repositories, sync branches (local or remote). To clean those up:

```bash
# In each repo:
git branch -D lobster-sync
git push origin --delete lobster-sync
```

## Troubleshooting

### Service is loaded but daemon is not running

Check the launchd stderr log for errors:

```bash
cat ~/.lobster/logs/sync-stderr.log
```

Common causes:
- Missing `jq` (install with `brew install jq`)
- Missing config file (run `lobster-sync add <path>` to create one)
- Script not found (re-run the installer)

### Sync completes but push fails

Check that your GitHub token is valid:

```bash
# Test token
curl -s -H "Authorization: token $(security find-generic-password -s lobster-sync -a github-token -w)" \
  https://api.github.com/user | jq .login
```

### "No changes detected" on every cycle

This is normal if you haven't made any changes since the last sync. The daemon uses tree-hash comparison and only creates a new commit when the working tree differs.

### launchd restarts the daemon repeatedly

The plist has `ThrottleInterval` set to 10 seconds. If the daemon crashes immediately on start, check the stderr log. Common causes:
- Config parse error (validate JSON: `jq . ~/.lobster/sync-config.json`)
- Permission issues on `~/.lobster/`

### PATH issues (git not found)

The plist includes `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin` in PATH. If git is installed elsewhere, edit the plist:

```bash
# Edit the installed plist
vi ~/Library/LaunchAgents/com.lobster.sync.plist
# Reload
launchctl unload ~/Library/LaunchAgents/com.lobster.sync.plist
launchctl load ~/Library/LaunchAgents/com.lobster.sync.plist
```

## Architecture

This is **Layer 4** of the Lobster sync system. It wraps the core sync scripts from Layer 1:

- `lobster-sync-daemon.sh` -- Daemon loop that syncs all registered repos on interval
- `lobster-sync-repo.sh` -- Syncs a single repo using git plumbing (write-tree/commit-tree)

Layer 4 adds:
- **launchd integration** -- Runs the daemon as a macOS service (auto-start, keep-alive, log routing)
- **CLI wrapper** -- Manages repos, service, and logs from a single command
- **Installer/uninstaller** -- Sets up the directory structure, scripts, and config
- **Keychain integration** -- Secure token storage using macOS Keychain
