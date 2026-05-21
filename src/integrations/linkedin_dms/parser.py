#!/usr/bin/env python3
"""
Wave 1b: LinkedIn DM parser — create Knows edges from LinkedIn Messages.csv.

LinkedIn DM exchanges are strong social signals: if the owner has direct-messaged
someone on LinkedIn (or they've messaged the owner), there is a verified communication
channel with mutual consent. These are Knows edges (inferred=false, strength=0.8).

LinkedIn Messages.csv format (from LinkedIn Data Export):
    CONVERSATION ID,CONVERSATION TITLE,FROM,SENDER PROFILE URL,RECIPIENT PROFILE URLS,DATE,CONTENT

Usage:
    python3 -m lobster.integrations.linkedin_dms.parser \\
        --csv ~/messages/linkedin/Messages.csv \\
        --owner-id OWNER_ENTITY_ID

    python3 -m lobster.integrations.linkedin_dms.parser \\
        --csv ~/messages/linkedin/Messages.csv \\
        --owner-id abc123 \\
        --dry-run

The OWNER_ENTITY_ID is the Kissinger entity ID for the LinkedIn account owner.
Run `kissinger entity list --kind person --search "Owner Name"` to find it.
"""

import argparse
import csv
import datetime
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

DEFAULT_DB = Path.home() / ".kissinger" / "graph.db"
KNOWS_STRENGTH = 0.8


class ConversationPartner(NamedTuple):
    name: str
    linkedin_url: str  # normalized, e.g. "https://www.linkedin.com/in/someone"


class MatchResult(NamedTuple):
    partner: ConversationPartner
    entity_id: str
    entity_name: str
    match_method: str  # "linkedin_url_exact" | "name_fuzzy" | "name_exact"
    score: float


def _normalize_linkedin_url(url: str) -> str:
    """Normalize a LinkedIn profile URL to canonical form."""
    if not url:
        return ""
    url = url.strip().rstrip("/").lower()
    # Strip query params / tracking
    url = re.sub(r"\?.*$", "", url)
    # Ensure https
    if url.startswith("http://"):
        url = "https://" + url[7:]
    if not url.startswith("https://"):
        url = "https://" + url
    return url


def _simple_name_normalize(name: str) -> str:
    """Lowercase, strip punctuation/titles for fuzzy matching."""
    name = name.lower().strip()
    name = re.sub(r"[^\w\s]", "", name)
    name = re.sub(r"\s+", " ", name)
    return name


def parse_messages_csv(csv_path: Path, owner_linkedin_url: str) -> list[ConversationPartner]:
    """
    Parse LinkedIn Messages.csv and extract unique conversation partners.

    The owner's own LinkedIn URL is used to exclude self-edges.
    Returns a list of unique (name, linkedin_url) pairs — one per person the owner DM'd.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"Messages.csv not found: {csv_path}")

    owner_url_normalized = _normalize_linkedin_url(owner_linkedin_url) if owner_linkedin_url else ""

    partners: dict[str, ConversationPartner] = {}  # keyed by normalized linkedin_url or name

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        # Column names may vary slightly across LinkedIn export versions
        from_col = next((h for h in headers if "FROM" in h.upper()), None)
        sender_url_col = next((h for h in headers if "SENDER PROFILE URL" in h.upper()), None)
        recipient_url_col = next((h for h in headers if "RECIPIENT PROFILE URL" in h.upper()), None)
        conv_title_col = next((h for h in headers if "CONVERSATION TITLE" in h.upper()), None)

        if not from_col:
            raise ValueError(f"Could not find FROM column in {csv_path}. Headers: {headers}")

        for row in reader:
            sender_name = (row.get(from_col) or "").strip()
            sender_url = _normalize_linkedin_url(row.get(sender_url_col) or "")
            recipient_urls_raw = row.get(recipient_url_col) or ""

            # Skip rows from the owner themselves (outgoing messages)
            if owner_url_normalized and sender_url == owner_url_normalized:
                # The recipients are the conversation partners
                for url in recipient_urls_raw.split(","):
                    url = _normalize_linkedin_url(url.strip())
                    if url and url != owner_url_normalized and url not in partners:
                        # Try to get name from conversation title
                        title = (row.get(conv_title_col) or "").strip()
                        partners[url] = ConversationPartner(name=title or url, linkedin_url=url)
            elif sender_url and sender_url != owner_url_normalized:
                # Incoming message — sender is a conversation partner
                if sender_url not in partners:
                    partners[sender_url] = ConversationPartner(name=sender_name, linkedin_url=sender_url)
            elif not sender_url and sender_name:
                # No URL but we have a name — use name as key
                key = _simple_name_normalize(sender_name)
                if key not in partners:
                    partners[key] = ConversationPartner(name=sender_name, linkedin_url="")

    return list(partners.values())


def load_kissinger_entities(db_path: Path) -> list[dict]:
    """Load all person entities from Kissinger via pycozo."""
    try:
        import pycozo
    except ImportError:
        print("ERROR: pycozo not installed. Run: pip install pycozo", file=sys.stderr)
        sys.exit(1)

    db = pycozo.client.Client("sqlite", str(db_path), dataframe=False)
    try:
        result = db.run(
            "?[id, name, meta] := *entity{id, kind, name, meta}, kind == \"person\"",
            {},
        )
        entities = []
        for row in result["rows"]:
            entity_id, name, meta_raw = row
            meta = meta_raw if isinstance(meta_raw, dict) else (json.loads(meta_raw) if isinstance(meta_raw, str) else {})
            entities.append({
                "id": entity_id,
                "name": name,
                "meta": meta,
                "linkedin_url": _normalize_linkedin_url(meta.get("linkedin_url") or ""),
            })
        return entities
    finally:
        db.close()


def match_partners_to_entities(
    partners: list[ConversationPartner],
    entities: list[dict],
) -> tuple[list[MatchResult], list[ConversationPartner]]:
    """
    Fuzzy-match conversation partners against Kissinger entities.

    Matching priority:
    1. Exact LinkedIn URL match
    2. Exact name match (case-insensitive)
    3. Fuzzy name match (token overlap >= 0.8)

    Returns (matched, unmatched).
    """
    # Build lookup indexes
    url_index: dict[str, dict] = {}
    name_index: dict[str, dict] = {}
    for e in entities:
        if e["linkedin_url"]:
            url_index[e["linkedin_url"]] = e
        name_index[_simple_name_normalize(e["name"])] = e

    matched: list[MatchResult] = []
    unmatched: list[ConversationPartner] = []

    for partner in partners:
        # 1. LinkedIn URL exact match
        if partner.linkedin_url and partner.linkedin_url in url_index:
            e = url_index[partner.linkedin_url]
            matched.append(MatchResult(
                partner=partner,
                entity_id=e["id"],
                entity_name=e["name"],
                match_method="linkedin_url_exact",
                score=1.0,
            ))
            continue

        # 2. Exact name match
        norm_name = _simple_name_normalize(partner.name)
        if norm_name and norm_name in name_index:
            e = name_index[norm_name]
            matched.append(MatchResult(
                partner=partner,
                entity_id=e["id"],
                entity_name=e["name"],
                match_method="name_exact",
                score=0.95,
            ))
            continue

        # 3. Fuzzy name match via token overlap
        if norm_name:
            tokens = set(norm_name.split())
            best_score = 0.0
            best_entity = None
            for e in entities:
                e_tokens = set(_simple_name_normalize(e["name"]).split())
                if not tokens or not e_tokens:
                    continue
                overlap = len(tokens & e_tokens) / max(len(tokens), len(e_tokens))
                if overlap > best_score:
                    best_score = overlap
                    best_entity = e
            if best_score >= 0.8 and best_entity is not None:
                matched.append(MatchResult(
                    partner=partner,
                    entity_id=best_entity["id"],
                    entity_name=best_entity["name"],
                    match_method="name_fuzzy",
                    score=best_score,
                ))
                continue

        unmatched.append(partner)

    return matched, unmatched


def create_knows_edges(
    matched: list[MatchResult],
    owner_id: str,
    db_path: Path,
    dry_run: bool = False,
) -> tuple[int, int, list[str]]:
    """
    Create Knows edges from owner → each matched conversation partner.

    Returns (created, skipped, errors).
    """
    try:
        import pycozo
    except ImportError:
        print("ERROR: pycozo not installed.", file=sys.stderr)
        sys.exit(1)

    created = 0
    skipped = 0
    errors = []
    now_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if dry_run:
        return len(matched), 0, []

    db = pycozo.client.Client("sqlite", str(db_path), dataframe=False)
    try:
        for m in matched:
            target_id = m.entity_id
            if owner_id == target_id:
                skipped += 1
                continue

            # Check if a Knows edge already exists (either direction)
            try:
                existing = db.run(
                    "?[src, tgt] := *edge{source: src, target: tgt, relation}, "
                    "relation == \"knows\", "
                    "(src == $owner and tgt == $target) or (src == $target and tgt == $owner)",
                    {"owner": owner_id, "target": target_id},
                )
                if existing["rows"]:
                    skipped += 1
                    continue
            except Exception as e:
                errors.append(f"Check existing edge {owner_id[:8]}->{target_id[:8]}: {e}")
                continue

            # Create Knows edge: owner → partner
            try:
                db.run(
                    "?[source, target, relation, value_frame, strength, notes, inferred, how_they_know, created_at, updated_at] <- "
                    "[[$source, $target, $relation, $vf, $st, $notes, $inf, $how, $ca, $ua]] "
                    ":put edge {source, target, relation => value_frame, strength, notes, inferred, how_they_know, created_at, updated_at}",
                    {
                        "source": owner_id,
                        "target": target_id,
                        "relation": "knows",
                        "vf": "",
                        "st": KNOWS_STRENGTH,
                        "notes": "",
                        "inf": False,
                        "how": "LinkedIn DM exchange",
                        "ca": now_ts,
                        "ua": now_ts,
                    },
                )
                created += 1
            except Exception as e:
                errors.append(f"Create edge {owner_id[:8]}->{target_id[:8]}: {e}")
    finally:
        db.close()

    return created, skipped, errors


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse LinkedIn Messages.csv and create Knows edges in Kissinger"
    )
    parser.add_argument(
        "--csv",
        required=True,
        help="Path to LinkedIn Messages.csv export file",
    )
    parser.add_argument(
        "--owner-id",
        required=True,
        help="Kissinger entity ID for the LinkedIn account owner",
    )
    parser.add_argument(
        "--owner-linkedin-url",
        default="",
        help="Owner's LinkedIn profile URL (used to exclude self from partner list)",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help=f"Path to Kissinger graph.db (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report matches and edge count without making changes",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)
    db_path = Path(args.db)

    print(f"[linkedin-dms] CSV: {csv_path}")
    print(f"[linkedin-dms] Owner entity ID: {args.owner_id}")
    print(f"[linkedin-dms] Dry run: {args.dry_run}")
    print()

    # --- Parse Messages.csv ---
    print("[linkedin-dms] Parsing Messages.csv...")
    try:
        partners = parse_messages_csv(csv_path, args.owner_linkedin_url)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[linkedin-dms] Found {len(partners)} unique conversation partners")

    # --- Load Kissinger entities ---
    print("[linkedin-dms] Loading Kissinger person entities...")
    entities = load_kissinger_entities(db_path)
    print(f"[linkedin-dms] Loaded {len(entities)} person entities from Kissinger")

    # --- Match ---
    print("[linkedin-dms] Matching conversation partners to Kissinger entities...")
    matched, unmatched = match_partners_to_entities(partners, entities)
    print(f"[linkedin-dms] Matched: {len(matched)}, Unmatched: {len(unmatched)}")

    # --- Create edges ---
    print("[linkedin-dms] Creating Knows edges...")
    created, skipped, edge_errors = create_knows_edges(
        matched, args.owner_id, db_path, dry_run=args.dry_run
    )

    # --- Report ---
    print()
    print("=" * 60)
    print("[linkedin-dms] REPORT")
    print(f"  Messages.csv conversation partners: {len(partners)}")
    print(f"  Matched to Kissinger entities:      {len(matched)}")
    print(f"  Unmatched (not in Kissinger):       {len(unmatched)}")
    print(f"  Knows edges created:                {created}")
    print(f"  Edges skipped (already exists):     {skipped}")
    print(f"  Errors:                             {len(edge_errors)}")

    if matched:
        print()
        print("  Matched contacts:")
        for m in matched[:20]:
            print(f"    [{m.match_method:<22}] {m.partner.name:<40} → {m.entity_name} ({m.entity_id[:8]})")
        if len(matched) > 20:
            print(f"    ... and {len(matched) - 20} more")

    if unmatched:
        print()
        print("  Unmatched conversation partners (not in Kissinger):")
        for p in unmatched[:20]:
            print(f"    {p.name:<40} {p.linkedin_url}")
        if len(unmatched) > 20:
            print(f"    ... and {len(unmatched) - 20} more")

    if edge_errors:
        print()
        print("  Errors:")
        for e in edge_errors[:10]:
            print(f"    {e}")

    if args.dry_run:
        print()
        print("[linkedin-dms] DRY RUN — no changes made.")


if __name__ == "__main__":
    main()
