# Lobster Shop

**Shareable skills, tools, and integrations for your Lobster assistant.**

The Lobster Shop is a collection of add-ons that extend what Lobster can do. Each skill is a self-contained package that adds new capabilities — from managing your calendar to controlling your music.

## How It Works

1. **Browse** — Look through `INDEX.md` or ask Lobster `/shop` to see what's available
2. **Install** — Run the skill's install script: `bash lobster-shop/<skill>/install.sh`
3. **Activate** — Use `activate_skill` to enable the skill at runtime
4. **Use** — The skill injects its behavior and context into Lobster automatically

## What's a Skill?

Skills are **four-dimensional units** that compose at runtime:

| Layer | Directory | Purpose |
|-------|-----------|---------|
| **Behavior** | `behavior/` | Instructions for how/when to use the skill |
| **Context** | `context/` | Domain knowledge and background info |
| **Preferences** | `preferences/` | User-configurable settings with defaults |
| **Tooling** | `tools/` or `src/` | MCP servers and code |

```
lobster-shop/
├── README.md              # This file
├── INDEX.md               # Browse all available skills
├── skill-template/        # Reference template for creating skills
│   └── skill.toml         # Annotated manifest template
└── camofox-browser/       # Example skill
    ├── skill.toml         # Manifest (TOML preferred, JSON compat)
    ├── skill.json         # Legacy manifest (deprecated)
    ├── README.md          # User-facing description
    ├── install.sh         # One-command installer
    ├── behavior/          # Behavioral instructions
    │   └── system.md      # Core behavior
    ├── context/           # Domain context
    │   └── domain.md      # Background knowledge
    ├── preferences/       # Configurable settings
    │   ├── schema.toml    # Valid preference keys + types
    │   └── defaults.toml  # Default values
    └── src/               # The actual code
```

### The Manifest (`skill.toml`)

Every skill has a `skill.toml` (or legacy `skill.json`) that describes:

- **What it is** — Name, description, author, category
- **Activation** — Always-on, triggered by commands, or contextual
- **Layering** — Priority for composition ordering (0-100)
- **What it provides** — MCP tools, bot commands
- **Compatibility** — Skills it enhances or conflicts with
- **Dependencies** — Python/Node/system packages, API keys

See `skill-template/skill.toml` for the full annotated schema.

### Conditional Composition

Skills can include behavior files that activate only when another skill is co-active:

```
behavior/
├── system.md              # Always included
└── with-calendar.md       # Only included when "calendar" skill is also active
```

This enables rich cross-skill interactions without tight coupling.

### Installation

Each skill includes an `install.sh` that handles:

- Installing dependencies (pip packages, system packages)
- Creating config directories
- Registering MCP tools with Claude
- Guiding you through any manual setup (API keys, OAuth, etc.)

After installation, activate the skill via the `activate_skill` MCP tool.

## Creating a Skill

1. Copy `skill-template/skill.toml` to `lobster-shop/my-skill/skill.toml`
2. Fill in the manifest fields
3. Add `behavior/system.md` with usage instructions
4. Add `context/domain.md` with domain knowledge (optional)
5. Add `preferences/schema.toml` and `preferences/defaults.toml` (optional)
6. Add a `README.md` explaining what it lets users DO
7. Add an `install.sh` that sets everything up
8. Put your code in `src/` or `tools/`

### Design Guidelines

- **User-first descriptions** — Say what it lets you DO, not how it works
- **One-command install** — `bash install.sh` should handle everything possible
- **Self-contained** — Don't modify core Lobster files; add alongside them
- **Graceful failures** — Check for dependencies before assuming they exist
- **Clear manual steps** — If something can't be automated, explain it plainly
- **Priority-aware** — Set `[layering] priority` appropriately (50 is default)

## MCP Tools

The skill system exposes these tools:

| Tool | Description |
|------|-------------|
| `list_skills` | Browse available skills with install/active status |
| `activate_skill` | Activate a skill (mode: always/triggered/contextual) |
| `deactivate_skill` | Deactivate a skill |
| `get_skill_context` | Get assembled context from all active skills |
| `get_skill_preferences` | Get merged preferences for a skill |
| `set_skill_preference` | Set a preference value |

## Directory

See `INDEX.md` for the full list of available and upcoming skills.
