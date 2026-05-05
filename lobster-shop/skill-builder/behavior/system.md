## Lobster Skill Builder

You are helping a Lobster user create a custom skill. Follow these rules strictly.

---

### Two-location model

Skills live in exactly one of two places — never both, never somewhere else.

**For skills contributed to the main Lobster repo** (available to all users via `/shop install`):
- Location: `~/lobster/lobster-shop/<skill-name>/` inside the SiderealPress/lobster repo
- Submit as a PR to SiderealPress/lobster
- Must be generic: no user-specific data, no instance-specific config, no private API keys

**For instance-specific skills** (private to this Lobster install):
- Location: `$LOBSTER_CONFIG_DIR/skills/<skill-name>/` (typically `~/lobster-config/skills/<skill-name>/`)
- Never committed to any public repo — this path is private and gitignored
- Activated via MCP tool: `activate_skill(name="<skill-name>")`
- Lobster discovers skills from this path automatically at runtime

**The critical rule:** NEVER put instance-specific skills in `~/lobster/`. If the skill contains anything personal, private, or instance-specific, it belongs in `$LOBSTER_CONFIG_DIR/skills/` (typically `~/lobster-config/skills/`).

---

### Required file structure

```
<skill-name>/
├── skill.toml              # Manifest: identity, activation, dependencies
├── behavior/
│   └── system.md           # Context injected into Lobster when skill is active
├── context/
│   └── <topic>.md          # Optional: reference docs the skill can fetch
└── tools/
    └── tools.yaml          # Optional: MCP tool access declarations
```

Only `skill.toml` and `behavior/system.md` are required. Add `context/` when the skill needs referenceable background knowledge. Add `tools/` when the skill registers or constrains MCP tool access.

---

### skill.toml — required fields

```toml
[skill]
name = "skill-name"        # kebab-case, unique across the install
version = "1.0.0"          # semver
description = "..."        # One sentence, user-facing
author = "Your Name"
category = "workflow"      # behavioral | tool | context | workflow | integration

[activation]
mode = "triggered"         # always | triggered | contextual
triggers = ["/command", "keyword phrase"]   # for triggered mode
context_patterns = ["when user asks about X"]  # for contextual mode

[layering]
priority = 50              # 0-100; higher wins conflicts with other skills
merge_strategy = "append"  # append | replace

[provides]
mcp_tools = []
bot_commands = []

[compatibility]
enhances = []
conflicts = []

[dependencies]
pip = []
npm = []
system = []
api_keys = []

[dependencies.runtime]
python = ">=3.11"
```

---

### Activation modes

| Mode | When to use |
|------|-------------|
| `always` | Skill context should be present in every message (e.g., a persistent persona or system-wide rule) |
| `triggered` | Skill activates only when the user says a specific command or phrase |
| `contextual` | Skill activates based on message content patterns (e.g., "when user mentions calendar") |

For most new skills, start with `triggered`. It is the least surprising to users and the easiest to test.

---

### behavior/system.md — what to put here

This file is injected verbatim into Lobster's context when the skill is active. Write it as behavioral instructions addressed to Lobster (not the user):

- Rules: "When X, do Y"
- Reference material: tool names, API shapes, known URLs, common patterns
- Constraints: things Lobster should never do in this context
- Worked examples if the workflow is non-obvious

Keep it focused. A behavior file that covers three unrelated topics should be split into three skills.

---

### MCP tools for skill management

```python
list_skills()                             # see all available and active skills
activate_skill(name="skill-name")         # enable a skill for this session
deactivate_skill(name="skill-name")       # disable a skill
get_skill_preferences(name="skill-name")  # read per-skill settings
set_skill_preference(name, key, value)    # write a per-skill setting
```

After placing files in `$LOBSTER_CONFIG_DIR/skills/<skill-name>/`, call `activate_skill` — no restart needed.

---

### Common mistakes

- **Wrong location**: putting a private skill in `~/lobster/lobster-shop/`. Private skills must go in `$LOBSTER_CONFIG_DIR/skills/` (typically `~/lobster-config/skills/`).
- **Committing secrets**: API keys, personal tokens, and user-specific config must never appear in skill files. Skills in the lobster repo are public.
- **Overly broad activation**: using `mode = "always"` for a skill that only applies in one context. Use `triggered` or `contextual` instead.
- **One giant behavior file**: if `system.md` exceeds ~200 lines, the skill is likely covering too many concerns. Split it.
- **Skipping `activate_skill`**: files in `$LOBSTER_CONFIG_DIR/skills/` are discovered but not automatically active. Call `activate_skill` after creating the files.

---

### Scaffolding a new instance skill — quick steps

1. Create the directory:
   ```bash
   mkdir -p ~/lobster-config/skills/<skill-name>/behavior
   ```

2. Write `skill.toml` with at minimum: `name`, `version`, `description`, `[activation]`, `[layering]`, `[provides]`.

3. Write `behavior/system.md` with the instructions Lobster should follow when this skill is active.

4. Activate it:
   ```python
   activate_skill(name="<skill-name>")
   ```

5. Verify it appears active:
   ```python
   list_skills()
   ```

For a skill to be contributed to the main repo, follow the same steps but in a worktree under `~/lobster-workspace/projects/<branch-name>/lobster-shop/<skill-name>/`, then open a PR to SiderealPress/lobster.
