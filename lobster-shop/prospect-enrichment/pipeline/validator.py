"""
Validator — Pipeline Hygiene

Validates enrichment data against the Kissinger entity schema before writing.
Prevents writing malformed or incomplete data to the graph.

Usage:
    from pipeline.validator import validate_contact, ValidationResult

    result = validate_contact({
        "name": "Jane Smith",
        "title": "VP Supply Chain",
        "company": "Acme Corp",
        "source_url": "https://acme.com/team",
        "org_kissinger_id": "ent_abc123",
    })
    if not result.valid:
        print(result.errors)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------

# Maximum lengths for string fields (Kissinger entity constraints)
_MAX_NAME_LEN = 256
_MAX_TITLE_LEN = 256
_MAX_URL_LEN = 2048
_MAX_NOTE_LEN = 4096

# Valid ISO 8601 UTC timestamp pattern
_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Valid provenance confidence values
_VALID_CONFIDENCE = {"high", "medium", "low"}

# Valid enrichment goals
_VALID_GOALS = {"org_chart", "work_history", "connections"}

# SHA-256 hash prefix pattern
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# UUID v4 pattern
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass
class ValidationResult:
    """Result of validating a contact or provenance record."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.valid = False

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)


# ---------------------------------------------------------------------------
# Contact validation
# ---------------------------------------------------------------------------

def validate_contact(contact: dict[str, Any]) -> ValidationResult:
    """
    Validate a contact dict before writing to Kissinger.

    Required fields:
        - name: non-empty string
        - org_kissinger_id: non-empty string (Kissinger entity ID of parent org)

    Recommended fields (warnings if missing):
        - title: job title
        - source_url: where this contact was found

    Args:
        contact: Contact dict to validate.

    Returns:
        ValidationResult with valid=True if all required fields pass.
    """
    result = ValidationResult(valid=True)

    # --- Required: name ---
    name = contact.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        result.add_error("contact.name is required and must be a non-empty string")
    elif len(name) > _MAX_NAME_LEN:
        result.add_error(f"contact.name exceeds max length ({_MAX_NAME_LEN}): '{name[:50]}...'")

    # --- Required: org_kissinger_id ---
    org_id = contact.get("org_kissinger_id")
    if not org_id or not isinstance(org_id, str) or not org_id.strip():
        result.add_error("contact.org_kissinger_id is required for edge creation")

    # --- Optional but validated: title ---
    title = contact.get("title")
    if title is not None:
        if not isinstance(title, str):
            result.add_error("contact.title must be a string")
        elif len(title) > _MAX_TITLE_LEN:
            result.add_error(f"contact.title exceeds max length ({_MAX_TITLE_LEN})")
    else:
        result.add_warning("contact.title is missing — enrichment will have no title")

    # --- Optional but validated: source_url ---
    source_url = contact.get("source_url")
    if source_url is not None:
        if not isinstance(source_url, str):
            result.add_error("contact.source_url must be a string")
        elif len(source_url) > _MAX_URL_LEN:
            result.add_error(f"contact.source_url exceeds max length ({_MAX_URL_LEN})")
        elif not (source_url.startswith("http://") or source_url.startswith("https://")):
            result.add_warning(f"contact.source_url doesn't look like a URL: '{source_url[:100]}'")
    else:
        result.add_warning("contact.source_url is missing — provenance trail incomplete")

    return result


def validate_provenance(meta: list[dict[str, Any]]) -> ValidationResult:
    """
    Validate a provenance meta block before writing to Kissinger.

    Checks that all required provenance fields are present and correctly formatted.

    Args:
        meta: List of {key, value} meta dicts that will be written to a Kissinger entity.

    Returns:
        ValidationResult with valid=True if all provenance fields pass.
    """
    result = ValidationResult(valid=True)
    meta_dict = {m["key"]: m["value"] for m in meta}

    required_provenance_keys = {
        "provenance.source",
        "provenance.source_url",
        "provenance.enriched_at",
        "provenance.enriched_by",
        "provenance.pipeline_run_id",
        "provenance.confidence",
        "provenance.goal",
        "provenance.raw_response_hash",
    }

    # --- Check all required keys present ---
    for key in required_provenance_keys:
        if key not in meta_dict:
            result.add_error(f"Missing required provenance field: {key}")

    # --- Validate individual field formats ---

    source = meta_dict.get("provenance.source")
    if source and not isinstance(source, str):
        result.add_error("provenance.source must be a string")

    enriched_at = meta_dict.get("provenance.enriched_at")
    if enriched_at and not _ISO_UTC_RE.match(enriched_at):
        result.add_error(
            f"provenance.enriched_at must be ISO 8601 UTC (YYYY-MM-DDTHH:MM:SSZ), "
            f"got: '{enriched_at}'"
        )

    enriched_by = meta_dict.get("provenance.enriched_by")
    if enriched_by and enriched_by != "wallace":
        result.add_warning(
            f"provenance.enriched_by is '{enriched_by}', expected 'wallace' for automated runs"
        )

    run_id = meta_dict.get("provenance.pipeline_run_id")
    if run_id and not _UUID_RE.match(run_id):
        result.add_error(
            f"provenance.pipeline_run_id must be a UUID v4, got: '{run_id}'"
        )

    confidence = meta_dict.get("provenance.confidence")
    if confidence and confidence not in _VALID_CONFIDENCE:
        result.add_error(
            f"provenance.confidence must be one of {sorted(_VALID_CONFIDENCE)}, "
            f"got: '{confidence}'"
        )

    goal = meta_dict.get("provenance.goal")
    if goal and goal not in _VALID_GOALS:
        result.add_error(
            f"provenance.goal must be one of {sorted(_VALID_GOALS)}, got: '{goal}'"
        )

    raw_hash = meta_dict.get("provenance.raw_response_hash")
    if raw_hash and not _HASH_RE.match(raw_hash):
        result.add_error(
            f"provenance.raw_response_hash must match 'sha256:<64 hex chars>', "
            f"got: '{raw_hash[:50]}'"
        )

    return result


def validate_contacts_batch(contacts: list[dict[str, Any]]) -> list[tuple[dict, ValidationResult]]:
    """
    Validate a batch of contacts.

    Args:
        contacts: List of contact dicts.

    Returns:
        List of (contact, ValidationResult) tuples.
    """
    return [(c, validate_contact(c)) for c in contacts]


def filter_valid(contacts: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """
    Split contacts into valid and invalid lists.

    Args:
        contacts: List of contact dicts.

    Returns:
        (valid_contacts, invalid_contacts) tuple.
    """
    valid, invalid = [], []
    for contact in contacts:
        result = validate_contact(contact)
        if result.valid:
            valid.append(contact)
        else:
            invalid.append({"contact": contact, "errors": result.errors})
    return valid, invalid
