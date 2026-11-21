## spam-report Skill

Lobster can help Drew report a spam call or text to the FTC and FCC, and tells
him exactly what to do for the carrier-level spam report (7726). **This skill
does not and cannot silently guarantee anything was filed successfully** — see
"Known Limitations / Risk" below. Every run ends with an honest status per
channel, never an assumed success.

---

### What this skill actually automates (read this first)

| Channel | Automated? | How |
|---------|-----------|-----|
| FTC complaint (reportfraud.ftc.gov) | Yes, via Camofox browser automation | Fills and submits the multi-step wizard; captures confirmation if the site shows one |
| FCC complaint (consumercomplaints.fcc.gov) | Attempted via Camofox, but **frequently blocked** | Same approach, but the site sits behind a Cloudflare bot challenge that reliably blocks even page *views* (see Known Limitations) |
| Carrier 7726 ("report junk text") | **No — not automatable** | Lobster has no access to Drew's personal phone's SMS inbox. The skill only drafts the exact forward instructions; Drew must act from his own phone |
| Tracking log | Yes, fully automated | Durable YAML log at `~/messages/config/spam_reports.yaml` |
| State Attorney General complaints | Out of scope for v1 | Explicitly deferred — forms vary by state |

Neither FTC nor FCC publish a public API for *submitting* individual
complaints (both only expose read-only statistical/data APIs — see
`context/api_research.md`). Browser automation via Camofox is therefore the
only available path for the two automatable channels, which is why this skill
depends on `camofox-browser`.

---

### Trigger detection

Activate when the message contains any of:
- `/reportspam` or `/spamlog` (explicit commands)
- "report this as spam", "report this number", "file a spam complaint",
  "file a complaint about this call/text", "report this call", "that's spam,
  report it"
- A forwarded or described spam message that explicitly asks to be reported
  (phone number + message content/description present, plus a reporting
  request)

`/spamlog` alone (no new report) means: show whether a given number has been
reported before. Treat a bare phone number following `/spamlog` as a lookup
request, not a new report.

---

### Dispatcher behavior (main thread — 7-second rule)

When a report-spam trigger is received:

1. Extract from the message (ask Drew if genuinely missing — see "Required
   inputs" below):
   - The phone number that sent/called the spam (required)
   - The message content or a brief description of the call (required)
   - Category hint if obvious (e.g. "real estate wholesaler", "robocall",
     "phishing") — optional, improves FTC/FCC category selection
2. Check the tracking log for prior reports of this number (see
   `context/tracking_log.md` for the lookup logic) — this is fast file I/O,
   fine to do inline.
3. Reply immediately:
   - If first time seeing this number: `"On it — filing FTC/FCC complaints for +1 XXX-XXX-XXXX now. I'll also tell you what to forward to 7726."`
   - If repeat offender: `"On it — and heads up, +1 XXX-XXX-XXXX has been reported N time(s) before (last: DATE). Filing again now."`
4. Mark message processed via `send_reply(..., message_id=message_id)`
5. Spawn a background subagent (general-purpose) with the full filing prompt
   — see "Subagent: filing procedure" below. Do NOT attempt Camofox form-fill
   on the main thread; that always exceeds 7 seconds.
6. Return to `wait_for_messages()` immediately

**Required inputs** — if the phone number is missing entirely, ask Drew for
it before spawning anything (cannot file either complaint without it). If
only the description is thin (e.g. just "spam text, report it" with no
quoted content), proceed anyway — file with whatever description exists
rather than blocking on a round-trip; note the thin description in the log.

---

### Subagent: filing procedure

The subagent is responsible for the full pipeline for ONE report:

1. **Check repeat-offender status** (if not already done by dispatcher) —
   read `~/messages/config/spam_reports.yaml`, see `context/tracking_log.md`.
2. **File FTC complaint** — follow `behavior/camofox_filing.md` section "FTC
   complaint flow". Capture: whether submission completed, any confirmation
   number/text shown, a screenshot of the final confirmation page (or of
   wherever it stopped, if it failed).
3. **File FCC complaint** — follow `behavior/camofox_filing.md` section "FCC
   complaint flow". Same capture requirements. **Expect this to fail or stall
   at a bot-detection challenge** — treat that as a normal, expected outcome
   to report honestly, not a bug to silently retry forever (retry once, then
   stop and report "blocked").
4. **Draft the 7726 forward text** — see "Carrier 7726 report" below. This is
   pure text generation, no browser needed.
5. **Write the tracking log entry** — see `context/tracking_log.md` for the
   schema and the append logic. Write one entry per report, with per-channel
   status.
6. **Report back to Drew** — call `write_result` AND also directly message
   Drew (via `send_reply`, since this is a subagent closing out user-visible
   work) with:
   - FTC: confirmed filed + confirmation number, OR unconfirmed + screenshot
     attached, OR failed + reason
   - FCC: same three-way outcome
   - The exact 7726 forward instructions (always shown, every time)
   - A one-line repeat-offender note if applicable
   - Always attach the screenshot(s) captured, even on uncertain/failed runs —
     Drew needs to be able to verify what actually happened himself.

**Never report a channel as "filed" without positive evidence** (confirmation
text/number or an unambiguous "success" screenshot). If Camofox could not
produce that evidence, the correct status is `filed-unconfirmed` or
`not-attempted` / `blocked` — see the status vocabulary in
`context/tracking_log.md` — and the message to Drew must say "I submitted the
form but couldn't confirm it went through — here's a screenshot, please
check" rather than "Filed!".

---

### Carrier 7726 report (manual — always, no exceptions)

Lobster does not receive Drew's personal SMS. The number that got the spam is
Drew's own phone, which is neither the Telegram bot nor the Twilio number
behind `send_sms_reply`. This step is **always manual** and must always be
labeled that way — never mark it "filed" or "done" in the tracking log, only
`manual-pending` (not yet confirmed by Drew) or `manual-confirmed` (Drew told
Lobster he did it).

Draft text to send Drew (fill in placeholders):

```
To report +1 XXX-XXX-XXXX to your carrier as spam:

1. Open the text message from +1 XXX-XXX-XXXX on your phone.
2. Forward it to 7726 (spells "SPAM").
3. Your carrier will likely reply asking you to confirm the sender's
   number — reply with +1 XXX-XXX-XXXX if asked.

This has to be done from your phone — I don't have access to your SMS inbox.
Reply "done" or "I forwarded it" once you've done this and I'll update the
log.
```

If Drew later replies confirming he forwarded it, update the tracking log
entry's `carrier_7726.status` from `manual-pending` to `manual-confirmed` and
set `carrier_7726.confirmed_at` to the current timestamp. This is a fast,
direct update — no subagent needed for that follow-up.

---

### Known Limitations / Risk (MANDATORY reading before filing anything)

1. **FCC site is frequently/reliably blocked by bot detection.** Live-checked
   2026-11-21: `consumercomplaints.fcc.gov` returned a Cloudflare "Performing
   security verification" interstitial on a plain page load via Camofox's
   anti-detection browser, before any form was even reached. Treat FCC filing
   as "attempt it, expect it to fail some or most of the time, always verify."
2. **CAPTCHA risk on both sites is real and can appear at any step**, not
   just the first page. A wizard that looks clean at step 1 (as FTC's did in
   this research) can still gate a later step behind a CAPTCHA. Camofox
   cannot solve CAPTCHAs. If one appears, stop, screenshot it, and report
   "blocked by CAPTCHA at step N" rather than attempting to bypass it.
3. **Multi-step wizard fragility.** FTC's `/assistant` flow is a progressive
   wizard (confirmed: "25% of 100%" progress bar, category-selection radio
   buttons gating a disabled "Continue" button) — field requirements and
   available categories can change between runs. The behavior doc in
   `camofox_filing.md` describes the flow observed on 2026-11-21; if the
   actual flow has drifted, stop and report the mismatch rather than
   guessing at form semantics.
4. **Terms of Use ambiguity on automated/agent submission.** Neither site's
   publicly accessible pages found during this research state a clear
   per-complaint policy on submission via an authorized agent/assistant
   acting on the real complainant's behalf (as opposed to a fully
   independent bot or a third-party "community advocate" program, which the
   FTC does formally support via a separate signup). This is a genuine gray
   area, not a confirmed green light. Drew is the actual complainant and
   Lobster is acting as his assistant with his explicit request each time —
   but this skill must never be used to mass-file, auto-trigger without a
   specific user request, or file on behalf of anyone other than the user
   who invoked it.
5. **No silent success claims.** If Camofox cannot produce a confirmation
   screenshot or confirmation text after clicking submit, the result for
   that channel is `filed-unconfirmed`, not `filed-confirmed`. Always send
   Drew the screenshot of the submitted (or last-reached) page so he can
   independently verify.
6. **Always screenshot before submitting**, in addition to after, so Drew
   can review exactly what data was about to be sent in his name, not just
   the aftermath.

---

### Error handling

- **No phone number provided** — ask Drew for it before doing anything else.
- **FTC wizard structure has changed** (fields/flow don't match
  `camofox_filing.md`) — stop, screenshot, report the mismatch, do not guess.
- **CAPTCHA encountered** — stop, screenshot, report `blocked-captcha` for
  that channel, do not retry automatically.
- **FCC site unreachable / bot-challenge loop** — retry navigation once; if
  still blocked, report `blocked` for that channel and move on to the other
  channel + 7726 draft + log write. Never let an FCC failure block the FTC
  filing or the log write.
- **Camofox server down** — report both FTC and FCC as `not-attempted`,
  still draft the 7726 text and write the log entry with both channels
  `not-attempted`.
