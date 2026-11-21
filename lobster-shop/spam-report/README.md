# spam-report — Lobster Skill

Report a spam call or text to the FTC and FCC, and get exact instructions
for the carrier-level 7726 "SPAM" forward. Keeps a durable log so repeat
offenders can be cross-referenced.

**Read this before trusting it:** FTC filing is automated via browser
automation but unconfirmed success is reported as unconfirmed, never as a
silent success. FCC filing is attempted the same way but is frequently
blocked by the site's own bot-detection (confirmed live during development —
see `context/api_research.md`). Carrier 7726 reporting is **never**
automated — Lobster has no access to your personal phone's SMS inbox, so
this step is always "here's exactly what to forward" instructions, not an
action taken on your behalf. See `behavior/system.md` → "Known Limitations
/ Risk" for the full picture.

---

## Quick Start

### 1. Install

```bash
cd ~/lobster/lobster-shop/spam-report
bash scripts/install.sh
```

The installer:
- Creates `~/messages/config/` and `~/messages/receipts/spam/`
- Initializes `spam_reports.yaml`
- Registers the skill with Lobster's skill manager

### 2. Activate the skill

Via Telegram: tell Lobster to activate the spam-report skill.

Or via MCP:
```python
activate_skill(skill_name="spam-report", mode="triggered")
```

### 3. Report a spam call/text

```
report this as spam
+1 972-525-7265
"Hi, I'm looking to buy homes in your area for cash..."
```

Or explicitly:
```
/reportspam +19725257265 Real estate wholesaler cold text, unsolicited
```

---

## Commands

| Command | Description |
|---------|-------------|
| `/reportspam <number> <description>` | File FTC/FCC complaints, draft the 7726 forward, log the report |
| `/spamlog <number>` | Check whether a number has been reported before |

### Natural language triggers

- "report this as spam"
- "file a spam complaint"
- "report this number"
- "file a complaint about this call/text"

---

## What's Automated vs. Manual

| Channel | Status |
|---------|--------|
| **FTC** (reportfraud.ftc.gov) | Automated via Camofox browser automation. No public submission API exists (verified). Confirmed reachable and form-fillable in live testing, but success is only reported when a confirmation number/message is actually captured — otherwise reported as unconfirmed. |
| **FCC** (consumercomplaints.fcc.gov) | Attempted via Camofox, same approach. No public submission API exists (verified). **Frequently blocked** — live testing hit a Cloudflare bot-challenge before reaching any form. The skill always attempts it, reports the outcome honestly, and never blocks FTC filing or the tracking log write if FCC fails. |
| **Carrier 7726** | **Not automated.** Lobster has no access to your personal phone's SMS inbox. The skill drafts exact forward instructions every time; you forward it yourself. Tracking log marks this `manual-pending` until you confirm, never `done`. |
| **Tracking log** | Fully automated — durable YAML log, supports repeat-offender lookup. |
| **State AG complaints** | Out of scope for v1 (forms vary by state). |

---

## Tracking Log

`~/messages/config/spam_reports.yaml` — see `context/tracking_log.md` for
the full schema and status vocabulary. Screenshots of filing attempts are
saved to `~/messages/receipts/spam/`.

## CLI Repeat-Offender Check

```bash
bash scripts/check_repeat_offender.sh "+19725257265"
bash scripts/check_repeat_offender.sh "+19725257265" --json
```

---

## Architecture

```
spam-report/
├── skill.toml                         # Skill manifest (triggers, permissions, deps)
├── behavior/
│   ├── system.md                      # Dispatcher logic, trigger detection, risk disclosure
│   └── camofox_filing.md              # FTC/FCC browser-automation procedures
├── context/
│   ├── api_research.md                # FTC/FCC API research findings + ToS assessment
│   └── tracking_log.md                # Log schema + pure helper function reference
├── scripts/
│   ├── install.sh                     # Installer
│   └── check_repeat_offender.sh       # CLI lookup (read-only)
└── README.md                           # This file
```

Like `buy-things` and `print-this`, this skill is primarily a behavior layer
executed by an agent — no daemons, no background processes. It always
delegates the actual filing work to a background subagent per Lobster's
7-second rule, and never claims a result it cannot evidence.
