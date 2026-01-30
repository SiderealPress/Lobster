# Context Templates

These template files define the structure for your personal context directory. When you set up Hyperion with a private configuration repository, copy these templates to a `context/` directory within your private config.

## Purpose

The context directory builds a persistent understanding of you over time:

- **Goals** - What you're working toward
- **Projects** - What you're building
- **Values** - What matters to you
- **Habits** - Your routines and preferences
- **People** - Key relationships
- **Desires** - Wants, wishes, bucket list
- **Serendipity** - Random discoveries and inspirations

This context enables Hyperion to:
1. Better understand your brain dumps
2. Link new thoughts to existing goals/projects
3. Recognize mentions of people you know
4. Suggest connections between ideas
5. Prioritize based on your stated values

## Setup

### 1. Create Context Directory in Your Private Config

```bash
# Assuming your private config is at ~/hyperion-config
mkdir -p ~/hyperion-config/context

# Copy templates
cp ~/hyperion/context-templates/*.md ~/hyperion-config/context/
```

### 2. Fill in Your Context

Edit each file to add your actual information. The templates include examples (in HTML comments) to guide you.

```bash
# Edit each context file
nano ~/hyperion-config/context/goals.md
nano ~/hyperion-config/context/projects.md
# ... and so on
```

### 3. Configure Context Path

Add to your `config.env`:

```bash
# Path to your context directory
HYPERION_CONTEXT_DIR="${HYPERION_CONFIG_DIR}/context"
```

### 4. Commit Your Context

```bash
cd ~/hyperion-config
git add context/
git commit -m "Add personal context files"
git push
```

## File Descriptions

| File | Purpose | Update Frequency |
|------|---------|------------------|
| `goals.md` | Long-term vision, annual objectives, current sprints | Monthly |
| `projects.md` | Active, on-hold, and completed projects | Weekly |
| `values.md` | Core principles, priorities, decision frameworks | Quarterly |
| `habits.md` | Daily/weekly routines, preferences, time blocks | Monthly |
| `people.md` | Key relationships, contact info, important dates | As needed |
| `desires.md` | Wants, bucket list, aspirations not yet goals | Quarterly |
| `serendipity.md` | Random discoveries, inspirations, interesting finds | Daily |

## How Context is Used

### During Brain Dump Processing

When you send a brain dump, Hyperion:

1. **Loads relevant context** based on keywords detected
2. **Matches to goals/projects** - Links your thought to existing work
3. **Identifies people** - Recognizes names you mention
4. **Checks alignment** - Notes if thought relates to stated values
5. **Suggests connections** - Links to related past brain dumps

### Example

**Your brain dump:**
> "Had a thought about the authentication system... maybe we should use OAuth instead of building our own. Also need to call Mike about the hiking trip."

**Context used:**
- `projects.md` - Finds "ProjectName" with "authentication" as current focus
- `people.md` - Identifies "Mike" as friend interested in hiking
- `goals.md` - Links to "Q1 Focus: Ship v1.0"

**Result:**
Issue created with:
- Label: `project:projectname`
- Related issue link: Previous auth discussion
- Reminder added: Call Mike (if reminders enabled)

## Privacy

Your context files contain personal information. Always:

1. Store in a **private** repository
2. Set restrictive permissions: `chmod 600 ~/hyperion-config/context/*`
3. Never commit sensitive data (passwords, etc.)
4. Review before sharing any backups

## Context Updates

The brain-dumps agent can suggest updates to your context:

- New project mentioned repeatedly? Suggests adding to `projects.md`
- New person mentioned? Offers to add to `people.md`
- Goal achieved? Flags for moving to completed

These suggestions appear as comments on the brain-dump issue, not automatic updates (you remain in control).

## Tips

1. **Start small** - Fill in just the essentials first
2. **Be honest** - Context works best when it reflects reality
3. **Review regularly** - Outdated context is worse than no context
4. **Use examples** - The commented examples show the expected format
5. **Evolve over time** - Add detail as you use the system more
