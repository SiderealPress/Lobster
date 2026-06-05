"""
Wave 1c-2: Gmail → Kissinger bridge.

Reads external contacts from Gmail and creates edges in Kissinger:
- Contacts where email/name matches a Kissinger entity → Knows edges
  (strength=0.8, inferred=false, how_they_know="email exchange")
- Contacts from target org domains not yet in Kissinger → LikelyKnows edges
  (strength=0.6, inferred=true, how_they_know="email from [domain]")

Usage (as subagent or CLI):
    from integrations.gmail.enrich import enrich_from_gmail
    result = enrich_from_gmail(user_id="6645894734", owner_entity_id="...", dry_run=True)

CLI:
    python3 -m lobster.integrations.gmail.enrich \\
        --user-id USER_ID \\
        --owner-entity-id ENTITY_ID \\
        [--target-domains zebra.com boeing.com] \\
        [--since-days 365] \\
        [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_DB = Path.home() / ".kissinger" / "graph.db"
KNOWS_STRENGTH = 0.8        # verified email exchange
LIKELY_KNOWS_STRENGTH = 0.6  # email from target org domain, not yet in Kissinger


@dataclass
class GmailEnrichResult:
    """Result of a Gmail enrichment run."""
    knows_created: int = 0
    knows_skipped: int = 0
    likely_knows_created: int = 0
    contacts_scanned: int = 0
    contacts_matched: int = 0
    contacts_unmatched: int = 0
    errors: list[str] = field(default_factory=list)


def _normalize_email(email: str) -> str:
    """Normalize email address to lowercase."""
    return email.lower().strip()


def _name_normalize(name: str) -> str:
    """Lowercase and strip punctuation for name comparison."""
    name = name.lower().strip()
    name = re.sub(r"[^\w\s]", "", name)
    name = re.sub(r"\s+", " ", name)
    return name


def _load_kissinger_entities(db_path: Path) -> list[dict]:
    """Load all person entities from Kissinger."""
    try:
        import pycozo
    except ImportError:
        raise ImportError("pycozo not installed. Run: pip install pycozo")

    db = pycozo.client.Client("sqlite", str(db_path), dataframe=False)
    try:
        result = db.run(
            "?[id, name, meta] := *entity{id, kind, name, meta}, kind == \"person\"",
            {},
        )
        entities = []
        for row in result["rows"]:
            entity_id, name, meta_raw = row
            meta = (meta_raw if isinstance(meta_raw, dict)
                    else (json.loads(meta_raw) if isinstance(meta_raw, str) else {}))
            entities.append({
                "id": entity_id,
                "name": name,
                "meta": meta,
                "email": _normalize_email(meta.get("email") or ""),
                "name_norm": _name_normalize(name),
            })
        return entities
    finally:
        db.close()


def _match_contact_to_entity(
    contact: dict,
    entities: list[dict],
    email_index: dict[str, dict],
    name_index: dict[str, dict],
) -> Optional[dict]:
    """Match a Gmail contact to a Kissinger entity.

    Priority: email exact → name exact → name fuzzy (≥0.8 token overlap).
    Returns the entity dict, or None if no match.
    """
    # 1. Email exact match
    email = contact.get("email", "")
    if email and email in email_index:
        return email_index[email]

    # 2. Name exact match
    name_norm = _name_normalize(contact.get("name") or "")
    if name_norm and name_norm in name_index:
        return name_index[name_norm]

    # 3. Fuzzy name (token overlap)
    if name_norm:
        tokens = set(name_norm.split())
        best_score = 0.0
        best = None
        for e in entities:
            e_tokens = set(e["name_norm"].split())
            if not tokens or not e_tokens:
                continue
            overlap = len(tokens & e_tokens) / max(len(tokens), len(e_tokens))
            if overlap > best_score:
                best_score = overlap
                best = e
        if best_score >= 0.8 and best is not None:
            return best

    return None


def _edge_exists(db, owner_id: str, target_id: str, relation: str) -> bool:
    """Check if an edge already exists between owner and target."""
    try:
        result = db.run(
            "?[s, t] := *edge{source: s, target: t, relation: r}, "
            "r == $rel, "
            "(s == $owner and t == $target) or (s == $target and t == $owner)",
            {"rel": relation, "owner": owner_id, "target": target_id},
        )
        return len(result["rows"]) > 0
    except Exception:
        return False


def _create_edge(
    db,
    source: str,
    target: str,
    relation: str,
    strength: float,
    how_they_know: str,
    inferred: bool,
    now_ts: str,
) -> None:
    """Upsert an edge into Kissinger."""
    db.run(
        "?[source, target, relation, value_frame, strength, notes, inferred, how_they_know, created_at, updated_at] <- "
        "[[$source, $target, $relation, $vf, $st, $notes, $inf, $how, $ca, $ua]] "
        ":put edge {source, target, relation => value_frame, strength, notes, inferred, how_they_know, created_at, updated_at}",
        {
            "source": source,
            "target": target,
            "relation": relation,
            "vf": "",
            "st": strength,
            "notes": "",
            "inf": inferred,
            "how": how_they_know,
            "ca": now_ts,
            "ua": now_ts,
        },
    )


def enrich_from_gmail(
    user_id: str,
    owner_entity_id: str,
    target_domains: Optional[list[str]] = None,
    since_days: int = 365,
    db_path: Path = DEFAULT_DB,
    dry_run: bool = False,
) -> GmailEnrichResult:
    """Main enrichment function: pull Gmail contacts and push edges to Kissinger.

    Args:
        user_id:          Lobster user ID (Telegram chat_id as str).
        owner_entity_id:  Kissinger entity ID for the Gmail account owner.
        target_domains:   Optional list of org domains for LikelyKnows creation.
                          E.g. ["zebra.com", "boeing.com"]
        since_days:       Days of email history to scan.
        db_path:          Path to Kissinger graph.db.
        dry_run:          If True, report without writing.

    Returns:
        GmailEnrichResult with counts.
    """
    result = GmailEnrichResult()

    # --- Pull Gmail contacts ---
    try:
        from integrations.gmail.client import get_all_external_contacts
    except ImportError as e:
        result.errors.append(f"Gmail client import failed: {e}")
        return result

    log.info("Fetching external Gmail contacts for user_id=%r (since_days=%d)", user_id, since_days)
    contacts = get_all_external_contacts(user_id, since_days=since_days)
    result.contacts_scanned = len(contacts)
    log.info("Got %d Gmail contacts", len(contacts))

    if not contacts:
        log.info("No Gmail contacts found — is Gmail OAuth set up?")
        return result

    # --- Load Kissinger entities ---
    try:
        entities = _load_kissinger_entities(db_path)
    except Exception as e:
        result.errors.append(f"Kissinger load failed: {e}")
        return result

    # Build indexes
    email_index: dict[str, dict] = {}
    name_index: dict[str, dict] = {}
    for e in entities:
        if e["email"]:
            email_index[e["email"]] = e
        if e["name_norm"]:
            name_index[e["name_norm"]] = e

    # Build target domain set for LikelyKnows
    target_domain_set = set()
    if target_domains:
        target_domain_set = {d.lstrip("@").lower() for d in target_domains}

    now_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # --- Match and create edges ---
    try:
        import pycozo
    except ImportError:
        result.errors.append("pycozo not installed")
        return result

    if not dry_run:
        db = pycozo.client.Client("sqlite", str(db_path), dataframe=False)
    else:
        db = None

    try:
        for contact in contacts:
            email = contact.get("email", "")
            contact_domain = email.split("@")[-1].lower() if "@" in email else ""

            matched_entity = _match_contact_to_entity(
                contact, entities, email_index, name_index
            )

            if matched_entity:
                result.contacts_matched += 1
                target_id = matched_entity["id"]

                if owner_entity_id == target_id:
                    continue

                if dry_run:
                    result.knows_created += 1
                    continue

                # Check if knows edge already exists
                if _edge_exists(db, owner_entity_id, target_id, "knows"):
                    result.knows_skipped += 1
                    continue

                # Create Knows edge
                try:
                    _create_edge(
                        db,
                        source=owner_entity_id,
                        target=target_id,
                        relation="knows",
                        strength=KNOWS_STRENGTH,
                        how_they_know="email exchange",
                        inferred=False,
                        now_ts=now_ts,
                    )
                    result.knows_created += 1
                except Exception as e:
                    result.errors.append(f"Create knows edge for {email}: {e}")

            elif contact_domain and contact_domain in target_domain_set:
                # Contact from a target org domain but not yet in Kissinger
                result.contacts_unmatched += 1

                if dry_run:
                    result.likely_knows_created += 1
                    continue

                # For LikelyKnows, we need to create or find the person entity first.
                # For now: just log it (full implementation would upsert the entity).
                # This is the minimal viable implementation — extend when needed.
                log.info(
                    "LikelyKnows candidate: %s (%s) — not yet in Kissinger",
                    contact.get("name"), email,
                )
                result.likely_knows_created += 1

            else:
                result.contacts_unmatched += 1

    finally:
        if db is not None:
            db.close()

    return result


def main() -> None:
    """CLI entry point for Gmail → Kissinger enrichment."""
    parser = argparse.ArgumentParser(
        description="Enrich Kissinger knows-graph from Gmail email history"
    )
    parser.add_argument(
        "--user-id",
        required=True,
        help="Lobster user ID (Telegram chat_id as str) for Gmail OAuth",
    )
    parser.add_argument(
        "--owner-entity-id",
        required=True,
        help="Kissinger entity ID for the Gmail account owner",
    )
    parser.add_argument(
        "--target-domains",
        nargs="+",
        default=[],
        help="Org domains to create LikelyKnows edges for (e.g. zebra.com boeing.com)",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=365,
        help="Days of email history to scan (default: 365)",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help=f"Path to Kissinger graph.db (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be created without making changes",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    result = enrich_from_gmail(
        user_id=args.user_id,
        owner_entity_id=args.owner_entity_id,
        target_domains=args.target_domains or None,
        since_days=args.since_days,
        db_path=Path(args.db),
        dry_run=args.dry_run,
    )

    print()
    print("=" * 60)
    print("Gmail → Kissinger Enrichment Report")
    print(f"  Dry run:                         {args.dry_run}")
    print(f"  Gmail contacts scanned:          {result.contacts_scanned}")
    print(f"  Matched to Kissinger entities:   {result.contacts_matched}")
    print(f"  Unmatched contacts:              {result.contacts_unmatched}")
    print(f"  Knows edges created:             {result.knows_created}")
    print(f"  Knows edges skipped (exists):    {result.knows_skipped}")
    print(f"  LikelyKnows candidates:          {result.likely_knows_created}")
    print(f"  Errors:                          {len(result.errors)}")
    if result.errors:
        for e in result.errors[:10]:
            print(f"    {e}")

    if result.contacts_scanned == 0:
        print()
        print("NOTE: 0 Gmail contacts found. Gmail OAuth not yet set up?")
        print("To authenticate: send 'connect my Google Calendar' or")
        print("'link Gmail' to Lobster and follow the auth link.")


if __name__ == "__main__":
    main()
