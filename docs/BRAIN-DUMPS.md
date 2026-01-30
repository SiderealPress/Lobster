# Brain Dumps Agent

The brain-dumps agent captures voice note "brain dumps" - unstructured streams of consciousness, ideas, and thoughts - and saves them to a dedicated GitHub repository with intelligent context linking.

## Overview

When you send a voice message to Hyperion that contains random thoughts, ideas, or musings rather than a specific command or question, the brain-dumps agent:

1. **Transcribes** the voice message (using local whisper.cpp)
2. **Triages** - Classifies type, extracts entities, assesses priority
3. **Matches context** - Links to your goals, projects, people, and past brain dumps
4. **Enriches** - Adds labels, action items, and suggested next steps
5. **Updates context** - Suggests additions to your personal context files
6. **Saves** as a GitHub issue with full context linking

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

---

## Staged Processing Pipeline

The brain-dumps agent processes each dump through four stages:

### Stage 1: Triage

Classifies the brain dump and extracts structure:

- **Type classification**: idea, task, note, question, reflection, desire, serendipity
- **Entity extraction**: people, projects, topics, dates, locations
- **Priority assessment**: urgency (urgent/soon/someday) and importance (high/medium/low)

### Stage 2: Context Matching

Connects the brain dump to your persistent context (if configured):

- **Project matching**: Links mentions to your active projects
- **People matching**: Identifies people from your contacts
- **Goal alignment**: Notes which goals the brain dump relates to
- **Related brain dumps**: Finds similar past issues

### Stage 3: Enrichment

Adds value through labels, links, and action items:

- **Labels**: Type, topic, project, and priority labels
- **Links**: To related issues, project repos, and external resources
- **Action items**: Extracted todos as checkboxes
- **Next steps**: AI-suggested follow-up actions

### Stage 4: Context Update

Identifies potential updates to your personal context:

- Detects new projects, people, desires, or goals
- Suggests additions (doesn't auto-update)
- Tracks patterns across brain dumps

---

## Setup

### Basic Setup (No Context)

The brain-dumps feature works out of the box without personal context:

1. **Enable the feature** (enabled by default):
   ```bash
   # In config/hyperion.conf
   HYPERION_BRAIN_DUMPS_ENABLED=true
   HYPERION_BRAIN_DUMPS_REPO=brain-dumps
   ```

2. **Ensure GitHub authentication** is configured

3. **Send a brain dump** - The repository is created automatically on first use

### Advanced Setup (With Personal Context)

For full context-aware processing, set up the context directory:

1. **Create context directory in your private config:**
   ```bash
   mkdir -p ~/hyperion-config/context
   ```

2. **Copy context templates:**
   ```bash
   cp ~/hyperion/context-templates/*.md ~/hyperion-config/context/
   ```

3. **Fill in your context files:**
   - `goals.md` - Your objectives and targets
   - `projects.md` - Active and past projects
   - `values.md` - Core principles and priorities
   - `habits.md` - Routines and preferences
   - `people.md` - Key relationships
   - `desires.md` - Wants and aspirations
   - `serendipity.md` - Random discoveries

4. **Configure context path:**
   ```bash
   # In config/hyperion.conf or config.env
   HYPERION_CONTEXT_DIR="${HYPERION_CONFIG_DIR}/context"
   ```

5. **Apply the configuration:**
   ```bash
   cd ~/hyperion && ./install.sh
   ```

See [context-templates/README.md](../context-templates/README.md) for detailed setup instructions.

---

## Usage

### Sending a Brain Dump

Simply send a voice message to Hyperion with your thoughts. The agent automatically detects brain dumps.

**Explicit triggers** (guaranteed detection):
- Start with "Brain dump:"
- Include "note to self"
- Say "thinking out loud"

**Implicit detection** (agent analyzes content):
- Multiple unrelated topics
- Stream of consciousness style
- No clear question or command

### Example Flow (With Context)

1. **You send voice message:**
   > "Brain dump: Been thinking about the auth system for ProjectX. Maybe we should use OAuth instead of rolling our own. Oh, and need to call Mike about the hiking trip next weekend."

2. **Agent processes through all stages:**
   - Triage: Type=task, Urgency=soon, People=[Mike], Projects=[ProjectX]
   - Context: Matches ProjectX (active, auth focus), Mike (friend, hiking buddy)
   - Enrich: Labels, action items, links to related auth discussion (#12)
   - Update: No new entities detected

3. **Hyperion responds:**
   > Brain dump captured! Created issue #15 in your brain-dumps repo.
   >
   > Context matched:
   > - Project: ProjectX (auth system)
   > - Person: Mike (hiking friend)
   > - Related: #12 (previous auth discussion)
   >
   > Action items extracted:
   > - Research OAuth providers
   > - Call Mike re: hiking trip

4. **GitHub Issue created with full enrichment:**

```markdown
## Transcription

Been thinking about the auth system for ProjectX. Maybe we should use
OAuth instead of rolling our own. Oh, and need to call Mike about the
hiking trip next weekend.

## Triage

- **Type**: task
- **Urgency**: soon
- **Importance**: high

## Context Matches

### Projects
- **ProjectX** (In Development)
  - Current focus: Authentication system
  - Repo: https://github.com/user/projectx

### People
- **Mike** - Friend
  - Context: Hiking buddy, lives in Austin

### Related Brain Dumps
- #12 (previous auth discussion)

## Action Items

- [ ] Research OAuth providers (Auth0, Okta, Firebase Auth)
- [ ] Call Mike about hiking trip

## Suggested Next Steps

- Review OAuth options and compare pricing/features
- Check calendar for availability next weekend
- Consider linking this to ProjectX issue tracker

## Metadata

- **Recorded**: 2026-01-30 10:30:00 UTC
- **Duration**: 45 seconds
- **Processing**: Staged (triage -> context -> enrich -> update)

---
*Captured via Hyperion brain-dumps agent v2 (staged processing)*
```

---

## Labels

The agent applies multiple label types:

### Type Labels
| Label | Applied When |
|-------|--------------|
| `type:idea` | New concepts, inventions |
| `type:task` | Something to do |
| `type:note` | Information to remember |
| `type:question` | Research needed |
| `type:reflection` | Personal thoughts |
| `type:desire` | Wants, wishes |
| `type:serendipity` | Random discoveries |

### Topic Labels
| Label | Applied When |
|-------|--------------|
| `tech` | Technology, programming |
| `business` | Business strategy, startups |
| `personal` | Personal life |
| `creative` | Art, writing, music |
| `health` | Fitness, wellness |
| `finance` | Money, investments |
| `work` | Career, job |

### Project Labels
Format: `project:{name}` - Links to specific projects from your context.

### Priority Labels
| Label | Meaning |
|-------|---------|
| `urgent` | Needs attention within 48 hours |
| `review-soon` | Within a week |
| `someday` | No time pressure |

---

## Context Files

Your personal context enables intelligent matching. See [context-templates/](../context-templates/) for templates.

| File | Purpose | When Loaded |
|------|---------|-------------|
| `goals.md` | Long/short-term objectives | Ideas, business topics |
| `projects.md` | Active projects | Project names detected |
| `values.md` | Core principles | Always (lightweight) |
| `habits.md` | Routines, preferences | Time-related dumps |
| `people.md` | Key relationships | Names detected |
| `desires.md` | Wants, aspirations | Desire-type dumps |
| `serendipity.md` | Random discoveries | Serendipity-type dumps |

---

## Context Updates

The agent suggests context updates but doesn't auto-apply them. When it detects:

- **New project**: Mentioned but not in `projects.md`
- **New person**: Named with relationship context
- **New desire**: Expressed as want/wish
- **New goal**: Stated as objective

It adds a "Context Updates (Suggested)" section to the issue:

```markdown
## Context Updates (Suggested)

Based on this brain dump, consider updating your context:

- [ ] Add "NewProject" to projects.md (Status: Planning)
- [ ] Add "Jamie" to people.md (Contractor - design work)

Reply "update context" to apply these suggestions.
```

### Patterns

The agent also tracks patterns:

```markdown
## Patterns Noticed

- This is the 3rd brain dump mentioning "authentication" this week
- Mike appears in 5 recent dumps - consider updating his entry
```

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `HYPERION_BRAIN_DUMPS_ENABLED` | `true` | Enable/disable feature |
| `HYPERION_BRAIN_DUMPS_REPO` | `brain-dumps` | Repository name |
| `HYPERION_CONTEXT_DIR` | `${HYPERION_CONFIG_DIR}/context` | Context files location |

Set these in `config/hyperion.conf` or your private `config.env`.

---

## Customization

### Customizing the Agent

Override the agent definition via private config overlay:

```bash
# Copy default agent to your private config
cp ~/hyperion/.claude/agents/brain-dumps.md ~/hyperion-config/agents/brain-dumps.md

# Edit to customize
nano ~/hyperion-config/agents/brain-dumps.md
```

### Customization Ideas

- **Add custom labels**: Domain-specific labels (`client:acme`, `area:frontend`)
- **Modify triage criteria**: Adjust what counts as urgent
- **Change issue template**: Add/remove sections
- **Custom context matching**: Add domain-specific matching rules
- **Integration hooks**: Post to Slack, create calendar events

### Disabling the Feature

```bash
# In hyperion.conf
HYPERION_BRAIN_DUMPS_ENABLED=false
```

Or rename the agent file:
```bash
mv ~/hyperion-config/agents/brain-dumps.md ~/hyperion-config/agents/brain-dumps.md.disabled
```

---

## Privacy

- The brain-dumps repository is created as **private** by default
- Context files contain personal information - keep in private config repo
- Audio files are stored locally, not uploaded to GitHub
- You maintain full control - delete issues/context as needed
- Context updates require your explicit approval

---

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
┌─────────────────────────────────────┐
│  STAGE 1: TRIAGE                     │
│  - Classify type                     │
│  - Extract entities                  │
│  - Assess urgency/importance         │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│  STAGE 2: CONTEXT MATCHING           │
│  - Load relevant context files       │
│  - Match projects, people, goals     │
│  - Find related brain dumps          │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│  STAGE 3: ENRICHMENT                 │
│  - Apply labels                      │
│  - Extract action items              │
│  - Suggest next steps                │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│  STAGE 4: CONTEXT UPDATE             │
│  - Detect new entities               │
│  - Queue update suggestions          │
│  - Note patterns                     │
└─────────────────────────────────────┘
        │
        ▼
GitHub issue created + confirmation sent
```

---

## Troubleshooting

### Brain dump not detected

- Start explicitly with "Brain dump:" or "Note to self:"
- Short, focused messages may be interpreted as commands

### Context not matching

- Verify context files exist at `$HYPERION_CONTEXT_DIR`
- Check file format matches templates
- Entity names must be close matches (case-insensitive)

### Repository not created

- Check GitHub authentication: `gh auth status`
- Verify repo creation permissions
- Check for name conflicts

### Labels not applied

- Labels are created automatically if they don't exist
- Check repository permissions

---

## Future Enhancements

Planned improvements:

- **Context auto-update with approval**: Apply suggested updates via button press
- **Audio file upload**: Optional upload to GitHub releases
- **Brain dump threading**: Group related dumps automatically
- **Weekly/monthly digests**: Summary of brain dump patterns
- **Task extraction**: Auto-create tasks in task management system
- **Calendar integration**: Add mentioned events to calendar
- **Semantic search**: Find brain dumps by meaning, not just keywords
