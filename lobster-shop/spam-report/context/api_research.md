## FTC / FCC API Research Findings (checked 2026-11-21)

This skill was built after explicitly re-verifying whether a public
submission API exists for either agency, rather than assuming browser
automation was the only option. Findings below.

### FTC

- **No public API for submitting individual fraud/complaint reports.**
  `reportfraud.ftc.gov` is a web-form-only intake (the multi-step
  `/assistant` wizard).
- The FTC does publish developer-facing APIs at `ftc.gov/developer` and
  `api.ftc.gov/v0`, gated behind a Data.gov API key — but these are
  **read-only** data/statistics endpoints (Consumer Sentinel aggregate data,
  Do Not Call registry telemarketer-access data, FOIA/press data), not a
  complaint-submission interface.
- The FTC does offer a "Report Fraud Community Advocate" program, which
  issues a custom pre-filled link for advocates to help others submit
  reports — this is a web-link mechanism for human advocates, not a
  machine API, and is out of scope here (it's designed for advocates
  helping *other people's* reports, not for a personal assistant acting on
  one user's own behalf).
- **Conclusion: browser automation (Camofox) is the only viable path for
  individual complaint submission**, consistent with the issue's own
  research.

### FCC

- **No public API for submitting individual consumer complaints.**
  `consumercomplaints.fcc.gov` is a web-form-only intake.
- The FCC does publish a "Consumer Complaints Data API" / "Consumer Help
  Center API" (`fcc.gov/consumer-help-center-api`,
  `fcc.gov/consumer-complaints-center-data`) — these let you **query
  existing, already-filed complaint datasets** (filter/aggregate), not
  submit new ones.
- **Conclusion: browser automation is the only path here too** — but see
  the live finding below, which materially affects feasibility.

### Live Camofox check (2026-11-21)

- `reportfraud.ftc.gov/` and `reportfraud.ftc.gov/assistant` both loaded
  cleanly via Camofox with no bot-challenge interstitial. The `/assistant`
  wizard rendered its first step normally (category radio buttons, a
  progress bar reading "25% of 100%", a disabled "Continue" button that
  enables once a radio is selected).
- `consumercomplaints.fcc.gov/hc/en-us` returned a **Cloudflare
  "Performing security verification" challenge page** on a plain,
  unauthenticated load — before any form was reached. This is a strong,
  concrete signal that FCC complaint filing will be unreliable even with
  Camofox's bot-detection-resistant browser.

This asymmetry (FTC generally reachable, FCC frequently blocked) is baked
into `behavior/camofox_filing.md` and the "Known Limitations" section of
`behavior/system.md` — the skill treats FCC filing as "attempt it, expect
partial failure, always report honestly," not as an equally-reliable
channel to FTC.

### Terms of Use / automated-submission ambiguity

Neither site's publicly accessible pages found during this research state
an explicit, unambiguous policy on an authorized personal assistant
submitting a complaint via browser automation on behalf of the actual
complainant (as distinct from an independent bot acting with no real
complainant behind it, which both sites plainly intend to prevent via
CAPTCHA/bot-detection). This is a genuine gray area:

- The FTC's Privacy Act statement and site language are written assuming a
  human filling out the form themselves.
- The existence of the FTC's Community Advocate program shows the agency
  does accept *some* forms of third-party-assisted submission, but that
  program has its own registration/link mechanism — using it was out of
  scope for this skill (it's designed for advocates filing on behalf of
  *other* people, not a personal AI assistant filing on behalf of the one
  user who asked it to).
- No explicit "no bots, ever, even if a human directed you" clause was
  found, but none of this should be read as an affirmative green light
  either — it is simply undocumented for this specific use case.

**Practical stance taken in this skill:** only file when Drew explicitly
requests a report for a specific number/message, never proactively or in
bulk; always produce verifiable evidence (screenshots + any confirmation
text) of exactly what was submitted; never claim success without that
evidence. If GitHub/legal feedback surfaces a reason this is not
acceptable, the right fix is to gate this skill behind an explicit opt-in
confirmation step per submission (not implemented in v1 — flagged as a
possible follow-up in the PR).

### State Attorney General complaints

Explicitly out of scope for v1, per the issue. State AG complaint forms vary
by state (Texas AG, in Drew's case, given the example number's area code —
but this was not verified or built against). A future version could add a
state-lookup (area code → state → AG complaint URL) but that adds real
scope and was deliberately deferred.
