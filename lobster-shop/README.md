# Lobster Shop

**Shareable skills, tools, and integrations for your Lobster assistant.**

The Lobster Shop is a collection of add-ons that extend what Lobster can do. Each skill is a self-contained package that adds new capabilities — from managing your calendar to controlling your music.

## How It Works

1. **Browse** — Look through `INDEX.md` or ask Lobster `/shop` to see what's available
2. **Install** — Run the skill's install script: `bash lobster-shop/<skill>/install.sh`
3. **Use** — The skill registers its tools with Lobster automatically. Just ask Lobster to do the thing.

## What's a Skill?

A skill is a directory inside `lobster-shop/` that contains everything needed to add a new capability to Lobster:

```
lobster-shop/
├── README.md              # This file
├── INDEX.md               # Browse all available skills
└── google-calendar/       # Example skill
    ├── skill.json         # Manifest: what it does, what it needs
    ├── README.md          # User-facing description
    ├── install.sh         # One-command installer
    └── src/               # The actual code
```

### The Manifest (`skill.json`)

Every skill has a `skill.json` that describes:

- **What it is** — Name, description, author
- **What it adds** — MCP tools, bot commands, scheduled jobs
- **What it needs** — Python packages, system packages, API keys, config
- **How to set it up** — Automated install steps and any manual steps

### Installation

Each skill includes an `install.sh` that handles:

- Installing dependencies (pip packages, system packages)
- Creating config directories
- Registering MCP tools with Claude
- Guiding you through any manual setup (API keys, OAuth, etc.)

## Creating a Skill

Want to create a skill? Here's the minimum you need:

1. Create a directory: `lobster-shop/my-skill/`
2. Add a `skill.json` manifest (see `google-calendar/skill.json` for the format)
3. Add a `README.md` explaining what it lets users DO
4. Add an `install.sh` that sets everything up
5. Put your code in `src/`

### Manifest Format

```json
{
  "name": "my-skill",
  "version": "1.0.0",
  "description": "One sentence: what this lets you do",
  "author": "Your Name",
  "status": "available",
  "adds": {
    "mcp_tools": ["tool_name_1", "tool_name_2"],
    "bot_commands": ["/mycommand"],
    "scheduled_jobs": []
  },
  "dependencies": {
    "pip": ["some-package>=1.0"],
    "system": ["some-system-package"],
    "api_keys": [
      {
        "name": "MY_API_KEY",
        "description": "Get this from https://example.com/api",
        "required": true
      }
    ]
  },
  "setup": {
    "auto": [
      "pip install -r requirements.txt",
      "claude mcp add my-skill-server"
    ],
    "manual": [
      "Create an account at https://example.com",
      "Generate an API key"
    ]
  }
}
```

### Design Guidelines

- **User-first descriptions** — Say what it lets you DO, not how it works
- **One-command install** — `bash install.sh` should handle everything possible
- **Self-contained** — Don't modify core Lobster files; add alongside them
- **Graceful failures** — Check for dependencies before assuming they exist
- **Clear manual steps** — If something can't be automated, explain it plainly

## Directory

See `INDEX.md` for the full list of available and upcoming skills.
