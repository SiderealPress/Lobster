## Tracking Log — spam_reports.yaml

### File location

`~/messages/config/spam_reports.yaml` (same convention as buy-things'
`spend_log.yaml` — lives under the user's `~/messages/config/` directory,
not in the repo).

### Schema

```yaml
reports:
  - phone_number: "+19725257265"
    date: "2026-11-21"
    description: "Unsolicited text from a real-estate wholesaler asking to buy the house."
    message_content: "Hi, I'm looking to buy homes in your area for cash..."
    channels:
      ftc:
        status: "filed-confirmed"       # filed-confirmed | filed-unconfirmed | blocked-captcha | blocked | not-attempted
        confirmation: "123456789"        # confirmation/reference number, or null
        screenshot: "~/messages/receipts/spam/2026-11-21-ftc-19725257265.png"
      fcc:
        status: "blocked"
        confirmation: null
        screenshot: "~/messages/receipts/spam/2026-11-21-fcc-19725257265.png"
      carrier_7726:
        status: "manual-pending"         # manual-pending | manual-confirmed
        confirmed_at: null
    notes: "FCC blocked by Cloudflare bot-challenge; drafted 7726 forward text for Drew."
```

### Status vocabulary (exhaustive — do not invent new values)

**ftc / fcc channels:**
- `filed-confirmed` — submission completed AND a confirmation number or
  explicit success message/screenshot was captured
- `filed-unconfirmed` — submit was clicked but no confirmation evidence was
  found; outcome genuinely unknown
- `blocked-captcha` — a CAPTCHA appeared and the flow was stopped
- `blocked` — site was unreachable or stuck behind bot-detection (e.g. FCC's
  Cloudflare challenge) before the form could even be reached
- `not-attempted` — filing was not tried this run (e.g. Camofox was down)

**carrier_7726 channel:**
- `manual-pending` — instructions were drafted and sent to Drew; no
  confirmation yet that he forwarded the message
- `manual-confirmed` — Drew has explicitly confirmed he forwarded it to 7726

Never write `filed-confirmed`/`done`/`complete` for `carrier_7726` — that
channel is never automated, full stop.

### Reading the log / repeat-offender lookup (pure function pattern)

Prefer `scripts/check_repeat_offender.sh` (wraps the same logic) for a
shell-callable check. Inline Python equivalent, written as a pure function
that only takes the log path and a number and returns data — no hidden
state, no writes:

```python
from pathlib import Path
from typing import Any
import yaml


def normalize_phone(raw: str) -> str:
    """Normalize a phone number to a bare +1XXXXXXXXXX-style digits string
    for comparison. Pure function: same input always produces same output.
    """
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    return f"+{digits}" if digits else raw


def load_reports(log_path: Path) -> list[dict[str, Any]]:
    """Read the tracking log. Returns [] if missing or malformed —
    never raises. Pure with respect to its input path (no mutation)."""
    if not log_path.exists():
        return []
    try:
        data = yaml.safe_load(log_path.read_text()) or {}
    except yaml.YAMLError:
        return []
    return data.get("reports", []) or []


def find_prior_reports(reports: list[dict[str, Any]], phone_number: str) -> list[dict[str, Any]]:
    """Pure filter — no I/O. Returns all prior report entries for a number,
    normalized for comparison, sorted most-recent first."""
    target = normalize_phone(phone_number)
    matches = [r for r in reports if normalize_phone(r.get("phone_number", "")) == target]
    return sorted(matches, key=lambda r: r.get("date", ""), reverse=True)
```

Usage in the dispatcher/subagent:

```python
log_path = Path.home() / "messages/config/spam_reports.yaml"
reports = load_reports(log_path)
prior = find_prior_reports(reports, phone_number)
if prior:
    last = prior[0]
    # "+1 972-525-7265 has been reported 2 time(s) before (last: 2026-10-03)."
```

### Writing a new entry (isolated I/O boundary)

Keep the read-modify-write as the only side-effecting step; everything else
feeding into it (building the new entry dict) should be a pure function of
its inputs:

```python
from datetime import date


def build_report_entry(
    phone_number: str,
    description: str,
    message_content: str,
    ftc_result: dict[str, Any],
    fcc_result: dict[str, Any],
    report_date: str | None = None,
) -> dict[str, Any]:
    """Pure — builds the entry dict, does not touch the filesystem."""
    return {
        "phone_number": phone_number,
        "date": report_date or date.today().isoformat(),
        "description": description,
        "message_content": message_content,
        "channels": {
            "ftc": ftc_result,
            "fcc": fcc_result,
            "carrier_7726": {"status": "manual-pending", "confirmed_at": None},
        },
        "notes": "",
    }


def append_report(log_path: Path, entry: dict[str, Any]) -> None:
    """The one I/O boundary: read current state, append, write back."""
    data = yaml.safe_load(log_path.read_text()) if log_path.exists() else {}
    if not data:
        data = {}
    data.setdefault("reports", []).append(entry)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False))
```

### Confirming a manual carrier_7726 forward later

When Drew later replies "done" / "I forwarded it":

```python
def mark_carrier_confirmed(log_path: Path, phone_number: str, confirmed_at: str) -> None:
    """The one I/O boundary for this update. Finds the most recent
    matching entry and updates it in place."""
    data = yaml.safe_load(log_path.read_text()) if log_path.exists() else {}
    reports = data.get("reports", [])
    target = normalize_phone(phone_number)
    matching = [r for r in reports if normalize_phone(r.get("phone_number", "")) == target]
    if not matching:
        return
    most_recent = sorted(matching, key=lambda r: r.get("date", ""), reverse=True)[0]
    most_recent["channels"]["carrier_7726"]["status"] = "manual-confirmed"
    most_recent["channels"]["carrier_7726"]["confirmed_at"] = confirmed_at
    log_path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False))
```

### Screenshot storage

Screenshots go to `~/messages/receipts/spam/`, filename pattern
`YYYY-MM-DD-{channel}-{digits-only-phone}.png` (e.g.
`2026-11-21-ftc-19725257265.png`), mirroring buy-things' receipts
convention. Created by `scripts/install.sh` if missing.
