# Debug Mode — Agent Supplement

This file is loaded when `LOBSTER_DEBUG=true`. It extends base behavior with debug-specific rules.

## Install.sh Completeness Rule

Every PR that changes how Lobster runs must check `install.sh`.

Before writing the PR description, scan your diff for:
- **New hook files** → add hook registration to install.sh
- **New cron jobs** → add cron entry to install.sh
- **New service files** → add `systemctl enable` / `systemctl start` to install.sh
- **New env vars or config keys** → add default/placeholder to install.sh
- **New scripts called from services or cron** → verify install.sh creates or copies them

Ask yourself: "If someone ran `install.sh` fresh today, would this change be included?" If not, update install.sh in the same PR.

## Debug-Specific Agent Behavior

When `LOBSTER_DEBUG=true`:

- Be more verbose in `write_result` summaries — include what you checked, not just what you concluded
- Emit `write_observation(category="system_context", ...)` for non-obvious decisions or skipped steps
- Log unexpected conditions to `~/lobster-workspace/logs/observations.log` (category: `system_error`)

## PR Self-Check Prompt

After implementing any change, before opening the PR:

1. Does `install.sh` reflect every new hook, cron, service, or config this PR introduces?
2. Does the PR description explain *why* the change is needed, not just *what* it does?
3. Are tests updated or added for the new behavior?
