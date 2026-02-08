# Lobster Onboarding Guide

Welcome to Lobster -- your always-on AI assistant, reachable via Telegram.

## Quick Start

1. Open Telegram, find your Lobster bot, and send `/start`
2. Send any text message -- Lobster will reply
3. Send a photo, voice note, or file -- Lobster handles those too
4. That's it. Lobster is always listening.

## What Can I Do?

- **Ask anything** -- "What's the capital of Mongolia?"
- **Send a photo** -- "What's in this image?" (attach a photo)
- **Send a voice note** -- Speak naturally, Lobster transcribes locally
- **Brain dump** -- Record a stream-of-consciousness voice note; Lobster triages it into action items
- **Drop files** -- Sync large files via LobsterDrop (Syncthing)
- **Schedule tasks** -- "Every morning at 9am, summarize my GitHub PRs"
- **Work on code** -- "Work on issue #42" delegates to a coding agent
- **Change behavior** -- "Be more concise" or "Always reply in bullet points"

---

## 1. Send an Image

Send any photo through Telegram. Lobster sees the image and responds to it.

**How it works:**
- Send a photo directly (camera or gallery)
- Add a caption for context: "What breed is this dog?"
- Lobster downloads the full image, views it, and replies based on both the image and your caption
- Images sent as files (uncompressed) also work

**Tips:**
- Telegram compresses photos by default. To send full quality, use the paperclip icon and choose "File" instead of "Photo"
- Lobster can read text in images, identify objects, describe scenes, and more

---

## 2. Send a Voice Note

Hold the microphone button in Telegram to record a voice message. Lobster transcribes it locally and responds.

**How it works:**
- Record and send a voice note in Telegram
- Lobster transcribes it using whisper.cpp running on the server
- Transcription is fully local -- no audio is sent to any cloud API
- Lobster then responds to the transcribed content

**Tips:**
- Speak clearly; the small whisper model handles most accents well
- You can send voice notes for quick questions, dictation, or brain dumps

---

## 3. Brain Dumps

Brain dumps turn unstructured voice notes into organized GitHub issues with action items.

**What counts as a brain dump:**
- Multiple topics in one message
- Stream of consciousness ("I've been thinking about...")
- Phrases like "brain dump", "note to self", "thinking out loud"

**How it works:**
1. Record a voice note with your thoughts
2. Lobster transcribes it and detects it's a brain dump
3. A dedicated agent triages, categorizes, and extracts action items
4. A GitHub issue is created in your `brain-dumps` repo (e.g., `aeschylus/brain-dumps`)
5. You get a summary with action items back in Telegram

**Setup:**
- Brain dumps are enabled by default
- The `brain-dumps` GitHub repo is created automatically on first use
- For richer context matching (linking to your projects, people, goals), set up personal context files:
  ```
  mkdir -p ~/lobster-config/context
  cp ~/lobster/context-templates/*.md ~/lobster-config/context/
  ```
  Then fill in `goals.md`, `projects.md`, `people.md`, etc.

**Example:**
> You: (voice note) "Brain dump -- need to refactor the auth module, also call Mike about the hiking trip, and look into that caching bug"
>
> Lobster: "Brain dump captured! Created issue #15. Action items: refactor auth module, call Mike re: hiking trip, investigate caching bug."

---

## 4. Settings and Customization

Lobster's behavior is configured through a few key files:

| File | What it controls |
|------|-----------------|
| `CLAUDE.md` | Core behavior instructions, personality, response style |
| `config/lobster.conf` | Feature flags (brain dumps on/off, repo names) |
| `config/config.env` | Credentials (Telegram token, GitHub PAT) |

**Private config overlay:**
For persistent customizations that survive upgrades, create a private config directory:
```
mkdir ~/lobster-config
cp ~/lobster/config/config.env ~/lobster-config/config.env
export LOBSTER_CONFIG_DIR=~/lobster-config
```

You can also place a custom `CLAUDE.md` or custom agents in this directory. See `docs/CUSTOMIZATION.md` for the full guide.

---

## 5. Requesting Behavior Changes

You do not need to edit files to change how Lobster behaves. Just tell it in conversation.

**Examples:**
- "Be more concise in your replies"
- "Always format code blocks with the language specified"
- "Check on me every morning at 9am"
- "When I send a brain dump, also send me the full transcription"
- "Use markdown formatting in all replies"

Lobster can update its own configuration, create scheduled jobs, or adjust its response style based on your requests.

**How it works:**
- Simple style changes take effect immediately in conversation
- Persistent changes (scheduled jobs, behavior rules) get saved to configuration files
- You can always ask "What are your current settings?" to review

---

## 6. LobsterDrop (File Sync)

LobsterDrop uses Syncthing to sync files between your devices and the Lobster server. This bypasses Telegram's 20MB file limit and lets you share files bidirectionally.

**How it works:**
- A shared folder (`~/LobsterDrop` on the server) syncs with a folder on your laptop/phone
- Drop a file on one side, it appears on the other within seconds
- Lobster can read and work with files you drop in

**Setup on Mac:**
```bash
# Install Syncthing
brew install syncthing

# Start Syncthing
brew services start syncthing

# Open the GUI to pair devices
open http://localhost:8384
```

**Setup on the server (if not already done):**
```bash
~/lobster/scripts/upgrade.sh  # Select 'y' when prompted for Syncthing
```

**Pairing steps:**
1. Open Syncthing GUI on both your Mac (`http://localhost:8384`) and the server
2. On the server, click "Actions > Show ID" and copy the device ID
3. On your Mac, click "Add Remote Device" and paste the server's ID
4. Accept the pairing request on the server
5. Share the `LobsterDrop` folder between the two devices
6. Create `~/LobsterDrop` on your Mac as the local folder path

**Tips:**
- On iOS/Android, use the Syncthing app from the App Store / Play Store
- Files sync bidirectionally -- Lobster can also drop files for you to pick up
- Great for sharing large datasets, images, or documents that exceed Telegram's limits

---

## Getting Help

- Send "help" to Lobster in Telegram for a quick command reference
- Check `docs/` in the lobster repo for detailed guides
- File issues at https://github.com/SiderealPress/lobster/issues
