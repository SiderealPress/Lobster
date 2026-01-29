# Brain Dumps Agent

The brain-dumps agent automatically captures voice note "brain dumps" - unstructured streams of consciousness, ideas, and thoughts - and saves them to a dedicated GitHub repository for later review.

## Overview

When you send a voice message to Hyperion that contains random thoughts, ideas, or musings rather than a specific command or question, the brain-dumps agent can:

1. Transcribe the voice message (using local whisper.cpp)
2. Classify whether it's a "brain dump" vs. a regular message
3. Auto-detect topics and themes
4. Save it as a GitHub issue in your private brain-dumps repository
5. Apply relevant labels for easy categorization

## What is a Brain Dump?

A brain dump is distinguished from regular messages by its unstructured nature:

| Brain Dump | Regular Message |
|------------|-----------------|
| "I've been thinking about the architecture, maybe microservices would work, also need to remember groceries, and that export feature idea..." | "What's the weather today?" |
| "Brain dump: startup idea - connecting farmers with restaurants..." | "Create a reminder for 3pm" |
| "Note to self - should look into that caching issue and also the UI redesign..." | "Review PR #42" |

**Indicators the agent looks for:**
- Phrases like "brain dump", "note to self", "thinking out loud"
- Multiple unrelated topics in one message
- Stream of consciousness style
- Ideas and reflections rather than questions or commands

## Setup

### 1. Enable the Feature

The brain-dumps feature is enabled by default. To configure it, edit your `hyperion.conf`:

```bash
# Enable/disable brain dump processing
HYPERION_BRAIN_DUMPS_ENABLED=true

# Repository name (created under your GitHub username)
HYPERION_BRAIN_DUMPS_REPO=brain-dumps
```

### 2. GitHub Authentication

Ensure you have GitHub authentication configured for Hyperion. The agent uses the GitHub MCP tools which require proper authentication.

### 3. Repository Creation (Automatic)

The first time you send a brain dump, the agent will automatically:
- Create a private repository named `{your-username}/brain-dumps`
- Initialize it with a README
- Set up default labels for categorization

## Usage

### Sending a Brain Dump

Simply send a voice message to Hyperion with your thoughts. The agent will automatically detect if it's a brain dump.

**Explicit triggers** (guaranteed to be treated as brain dump):
- Start with "Brain dump:"
- Include "note to self"
- Say "thinking out loud"

**Implicit detection** (agent analyzes content):
- Multiple unrelated topics
- Stream of consciousness style
- No clear question or command

### Example Flow

1. **You send voice message:**
   > "Brain dump: Been thinking about the new product launch. We should probably do a soft launch first, maybe to the beta users. Also, the pricing model needs work - maybe freemium? And I need to call the accountant about Q4 taxes."

2. **Hyperion responds:**
   > Brain dump saved! Created issue #12 in your brain-dumps repo.
   >
   > Topics detected: product, business, finance
   >
   > View: https://github.com/yourname/brain-dumps/issues/12

3. **GitHub Issue created:**
   ```markdown
   # [Brain Dump] Product launch thoughts and Q4 taxes

   ## Transcription

   Been thinking about the new product launch. We should probably do a
   soft launch first, maybe to the beta users. Also, the pricing model
   needs work - maybe freemium? And I need to call the accountant about
   Q4 taxes.

   ## Metadata

   - **Recorded**: 2024-01-15 10:30:00 UTC
   - **Duration**: 45 seconds

   ## Auto-detected Topics

   - Product launch strategy
   - Pricing model considerations
   - Tax/accounting follow-up

   ---
   *Captured via Hyperion brain-dumps agent*
   ```

## Labels

The agent auto-applies labels based on content analysis:

| Label | Applied When |
|-------|--------------|
| `idea` | New ideas, concepts, inventions |
| `project` | Project-related thoughts |
| `personal` | Personal notes, life stuff |
| `work` | Work/career related |
| `creative` | Creative writing, art ideas |
| `tech` | Technology, programming |
| `business` | Business strategy, startup ideas |
| `finance` | Money, taxes, budgets |
| `review` | Needs follow-up or research |

Labels are created automatically if they don't exist in your repository.

## Repository Structure

Your brain-dumps repository will contain:

```
brain-dumps/
├── README.md           # Auto-generated explanation
└── (issues)            # Each brain dump is an issue
```

Issues can be:
- Searched by label
- Closed when processed
- Referenced in other projects
- Exported or archived

## Configuration Options

| Variable | Default | Description |
|----------|---------|-------------|
| `HYPERION_BRAIN_DUMPS_ENABLED` | `true` | Enable/disable the feature |
| `HYPERION_BRAIN_DUMPS_REPO` | `brain-dumps` | Repository name |

These variables are set in `config/hyperion.conf` or `config/hyperion.conf.example`.

## Customization via Private Config Overlay

The brain-dumps agent can be customized using Hyperion's [private config overlay system](CUSTOMIZATION.md).

### Overriding the Agent Definition

To customize the brain-dumps agent behavior, create your own version in your private config directory:

```bash
# Create custom agent in your private config
cp ~/hyperion/.claude/agents/brain-dumps.md ~/hyperion-config/agents/brain-dumps.md

# Edit to your preferences
nano ~/hyperion-config/agents/brain-dumps.md
```

When the installer runs, your custom `agents/brain-dumps.md` will be copied to `~/hyperion/.claude/agents/`, overriding the default.

### Customization Ideas

You can modify the agent to:

- **Change topic detection labels**: Add labels specific to your work (e.g., `client-a`, `project-x`)
- **Modify the issue template**: Add custom sections or formatting
- **Adjust classification criteria**: Be more or less strict about what counts as a brain dump
- **Add integrations**: Post to Slack, create tasks, etc.

### Disabling Brain Dumps

To disable the feature entirely, set in your `hyperion.conf`:

```bash
HYPERION_BRAIN_DUMPS_ENABLED=false
```

Or simply remove/rename the agent file:

```bash
mv ~/hyperion-config/agents/brain-dumps.md ~/hyperion-config/agents/brain-dumps.md.disabled
```

## Privacy

- The brain-dumps repository is created as **private** by default
- Audio files are stored locally, not uploaded to GitHub
- Only the transcription text appears in GitHub issues
- You maintain full control to delete issues as needed

## Integration with Hyperion

The brain-dumps agent integrates with Hyperion's main loop:

```
Voice message received
        │
        ▼
transcribe_audio() converts to text
        │
        ▼
Main agent detects potential brain dump
        │
        ▼
brain-dumps agent spawned via Task tool
        │
        ▼
Agent classifies, processes, saves to GitHub
        │
        ▼
Confirmation sent to user
```

## Troubleshooting

### Brain dump not detected

If your brain dump is being treated as a regular message:
- Start explicitly with "Brain dump:" or "Note to self:"
- The agent may interpret short, focused messages as commands

### Repository not created

Check:
- GitHub authentication is configured
- You have permission to create repositories
- The repository name doesn't conflict with an existing repo

### Labels not applied

The agent creates labels if they don't exist. If label creation fails:
- Check repository permissions
- Labels may need to be created manually once

## Future Enhancements

Planned improvements include:
- Audio file upload to GitHub (optional)
- Custom label definitions per user
- Brain dump threading (related dumps grouped together)
- Weekly/monthly summary digests
- Integration with task management (auto-create tasks from action items)
