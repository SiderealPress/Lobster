## Camofox Filing Procedures — FTC & FCC

Step-by-step instructions for filing complaints via browser automation.
Both procedures assume the standard Camofox tool surface:
`camofox_create_tab`, `camofox_navigate`, `camofox_snapshot`,
`camofox_type`, `camofox_click`, `camofox_screenshot`.

**Before starting either flow:** take a screenshot immediately before the
final submit click, and another immediately after, regardless of outcome.
Both get attached to the report Drew receives.

---

### FTC complaint flow (reportfraud.ftc.gov)

#### Step 1 — Open the assistant

```python
tab = camofox_create_tab(url="https://reportfraud.ftc.gov/assistant")
snapshot = camofox_snapshot(tabId=tab["tabId"])
```

Confirmed live on 2026-11-21: this loads directly with no bot-challenge
interstitial, straight into a progressive wizard. The page shows a progress
bar ("25% of 100%") and the first question:

> "Is your report about any of these common problems?"

with radio options including "Phone, internet, TV service", "Just an
annoying call", and "Something else (we'll get it to the right place)".

#### Step 2 — Pick the category

For spam calls/texts, prefer in this order based on content:
- Real estate / investment pitch, job offer, or similar unsolicited sales
  pitch via text → "Something else (we'll get it to the right place)" is
  usually safest if no category matches cleanly. If the message is a plain
  unwanted robocall/robotext with no scam angle, "Just an annoying call" is
  the better fit.
- Impersonation (fake business/government/person) → "An impersonator"
- Investment/job/money-making pitch (e.g. "make $$ flipping houses",
  wholesaler recruiting) → "Job, investment, money-making opportunity,
  franchise"

```python
camofox_click(tabId=tab["tabId"], ref=category_radio_ref)
camofox_snapshot(tabId=tab["tabId"])  # Continue button should now be enabled
camofox_click(tabId=tab["tabId"], ref=continue_button_ref)
```

The "Continue" button is disabled (`[disabled]` in the snapshot) until a
radio option is selected — always re-snapshot after clicking a radio to get
the now-enabled Continue ref before clicking it.

#### Step 3 — Follow the wizard

The wizard is multi-step and adaptive (questions depend on the category
chosen in Step 2). General rules for every subsequent step:

1. `camofox_snapshot()` to see the current question and fields.
2. Fill text fields with `camofox_type(ref, value)` using the phone number
   and description Drew provided.
3. For date/time of contact, use "today" / the date Drew mentioned, or if
   unspecified, use the date the message was sent/forwarded to Lobster.
4. For free-text "what happened" fields, write a concise factual summary:
   `"Received an unsolicited text message from <number> on <date>. Message
   content: '<content>'. <brief context, e.g. appears to be a real estate
   wholesaler soliciting property purchases>."`
5. Click "Continue" after each step; re-snapshot before the next interaction
   (refs are not stable across page transitions).
6. **If a CAPTCHA appears at any step** — stop immediately, screenshot it,
   and report `blocked-captcha` for this channel. Do not attempt to solve it
   or guess at a bypass.
7. **If contact info fields are requested** (name, email, address) — use
   only what's in Drew's message/config; never invent data. If required
   fields are missing and Drew hasn't provided them, leave them blank if the
   form allows, otherwise stop and ask Drew before continuing.

#### Step 4 — Review and submit

Most FTC complaint flows end with a review/summary screen before final
submission.

```python
camofox_screenshot(tabId=tab["tabId"])  # BEFORE submit — capture what will be sent
camofox_click(tabId=tab["tabId"], ref=submit_button_ref)
```

#### Step 5 — Capture confirmation

```python
snapshot = camofox_snapshot(tabId=tab["tabId"])
screenshot = camofox_screenshot(tabId=tab["tabId"])  # AFTER submit
```

Look in the snapshot text for:
- A confirmation/reference number (format varies — look for "report number",
  "confirmation number", or a standalone numeric/alphanumeric ID near a
  "thank you" or "submitted" heading)
- A clear success heading (e.g. "Thank you", "Your report has been
  submitted")

**If no confirmation number or clear success text is found** — status is
`filed-unconfirmed`, not `filed-confirmed`, even if the submit click appeared
to go through. Report this distinction honestly to Drew.

---

### FCC complaint flow (consumercomplaints.fcc.gov)

#### Step 1 — Open the complaint center

```python
tab = camofox_create_tab(url="https://consumercomplaints.fcc.gov/hc/en-us")
snapshot = camofox_snapshot(tabId=tab["tabId"])
```

**Confirmed live on 2026-11-21: this is frequently blocked.** The page
returned a Cloudflare interstitial — "Performing security verification...
This website uses a security service to protect against malicious bots" —
on a plain, unauthenticated page load, with no form interaction at all. This
persisted across a retried navigation in the same session.

#### Step 2 — Handle the bot challenge

```python
import time
time.sleep(5)
snapshot = camofox_snapshot(tabId=tab["tabId"])
# Check if the challenge cleared
```

Give it one retry with a short wait (Cloudflare challenges sometimes clear
automatically for real browsers after a few seconds). If the snapshot still
shows "Performing security verification" or any Cloudflare/Ray ID text after
the retry:

- Stop. Do not retry further (repeated hammering looks worse to the target
  site, not better).
- Report this channel as `blocked` with the reason "FCC site bot-challenge
  did not clear."
- Proceed with FTC filing, 7726 drafting, and the tracking log write
  regardless — an FCC block must never cascade into skipping the other
  channels.

#### Step 3 — If the challenge clears, find the complaint form

The informal-complaint entry point is typically linked from
`consumercomplaints.fcc.gov` or `fcc.gov/complaints`. If the challenge clears:

```python
camofox_navigate(tabId=tab["tabId"], url="https://consumercomplaints.fcc.gov/hc/en-us/requests/new")
snapshot = camofox_snapshot(tabId=tab["tabId"])
```

Look for a complaint-type selector (phone/unwanted calls category) and a
free-text description field. Fill using the same data as the FTC flow:
phone number, date, message content/description.

#### Step 4 — Review, screenshot, submit

Same discipline as FTC: screenshot before submit, screenshot after, check
for a confirmation number or explicit success message in the snapshot text
before marking `filed-confirmed`.

#### Step 5 — Report honestly

If the flow was blocked at Step 2, the report to Drew says exactly that:
"FCC complaint could not be filed — the site's bot-detection page never
cleared. You may want to file this one manually at
consumercomplaints.fcc.gov, or I can try again later." This is a legitimate,
expected outcome for this channel — not a failure of the skill.

---

### Shared discipline for both flows

- Never fabricate a confirmation number. If one isn't visible in the
  snapshot, there isn't one to report.
- Never mark a channel `filed-confirmed` without screenshot + extracted
  confirmation evidence attached to the result.
- Always send both the pre-submit and post-submit screenshots to Drew, not
  just the pass/fail status — he needs to be able to verify what was
  actually submitted in his name.
- If the form structure encountered doesn't match what's described here
  (fields renamed, steps reordered, new required fields), that means the
  site changed since this doc was last verified (2026-11-21) — stop, report
  the mismatch with a screenshot, and don't improvise data into fields whose
  purpose isn't clear from the label.
