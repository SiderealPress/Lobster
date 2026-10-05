# Per-user workspaces

A workspace holds the standing rules for one person (or client) that Lobster talks
to, keyed by chat id. The `inject-user-workspace.py` hook copies those rules into
every subagent prompt for that person, so they do not depend on the dispatcher
remembering them.

## Layout

```
~/lobster-user-config/workspaces/<slug>/
  workspace.json     machine-readable config (required)
  RULES.md           the human-edited rule list (single source of truth)
  box-state.json     {"active_box": "...", "since": "<ISO8601>", "history": [...]}
  boxes/<box>.md     optional per-box context, appended when that box is active
```

`workspace.json` fields used by the hook:

| field | meaning |
|---|---|
| `slug` | workspace id; used in the injection marker (defaults to dir name) |
| `chat_ids` | list of chat ids (strings) this workspace applies to |
| `rules_file` / `box_state_file` | file names, default `RULES.md` / `box-state.json` |
| `model_for` | non-empty enables model enforcement |
| `model_trigger_keywords` | words (word-boundary, optional plural) that require Opus |

Other fields (fonts, colors, linter path) are informational for humans and builders.

## The hook

`hooks/inject-user-workspace.py`, PreToolUse, matcher `Agent`. For a prompt whose
YAML frontmatter `chat_id` matches a workspace it:

1. Inserts, right after the frontmatter's closing `---`:
   `<!-- workspace-rules: <slug> -->`, `## Active box: <box> (since <ts>)`, the
   box file, RULES.md, `<!-- /workspace-rules -->`. Skipped if the marker exists.
2. Sets `model` to `opus` when prompt or description hits a trigger keyword, the
   current model is not Opus, and there is no `model_override_reason: <why>` line.
3. Appends `ts | slug | chat_id | injected=yes/no | model: old->new` to the log.

Prompts without frontmatter, unknown chat ids, and any error pass through
unchanged (exit 0). Rewriting uses `hookSpecificOutput.updatedInput`, verified
live on Claude Code 2.1.289 for both prompt and model.

## Environment variables

| var | default |
|---|---|
| `LOBSTER_WORKSPACES_DIR` | `~/lobster-user-config/workspaces` |
| `LOBSTER_WORKSPACE_INJECT_LOG` | `~/lobster-workspace/logs/workspace-inject.log` |
| `LOBSTER_WORKSPACE_INJECT_MODE` | `rewrite` (or `block`: exit 2 with what is missing) |

## Activation and rollback

Activate by adding `python3 ~/lobster/hooks/inject-user-workspace.py` to the
`PreToolUse` entry with matcher `Agent` in `~/.claude/settings.json`, after
`require-task-id-in-prompt.py`.

Rollback: remove the inject-user-workspace.py entry from the `~/.claude/settings.json`
PreToolUse Agent hooks. No other state needs to be undone.
