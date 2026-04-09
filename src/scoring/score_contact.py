"""
Contact scoring engine for Eloso CRM.

Produces a 0–100 score for each contact, indicating fit as a potential
customer or design partner for Eloso's AI-driven supply chain optimization
platform. Higher = better fit.

Scoring factors (six total, each 0–1 before weighting):
  1. title_relevance   — How well the title/role maps to Eloso's buyer/champion profiles
  2. seniority         — Seniority level within their organization
  3. org_type          — Whether their organization is a prospect, VC, etc.
  4. interaction_recency — How recently we've interacted (stale = penalized)
  5. network_proximity — Connected via ally/strong edge relationships
  6. record_completeness — How complete the contact record is

ICP context:
  - Primary champions: CSCO, VP/Director of Supply Chain, Demand Planning leads
  - Economic buyers: CEO, COO, CIO at backlog-intensive manufacturers
  - Target orgs: Aerospace, Heavy Equipment, Industrial, Capital Goods, Contract Mfg
  - Org size: $100M+ revenue, 200+ employees preferred
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Factor weights — must sum to 1.0
# ---------------------------------------------------------------------------

WEIGHTS = {
    "title_relevance": 0.30,
    "seniority": 0.25,
    "org_type": 0.20,
    "interaction_recency": 0.10,
    "network_proximity": 0.08,
    "record_completeness": 0.07,
}

assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "Weights must sum to 1.0"

# ---------------------------------------------------------------------------
# Title relevance
# ---------------------------------------------------------------------------

# High-value roles: Eloso's primary champions and economic buyers
_TITLE_HIGH = [
    r"\bcsco\b",
    r"\bchief supply chain\b",
    r"\bvp.{0,10}supply chain\b",
    r"\bvice president.{0,10}supply chain\b",
    r"\bdirector.{0,10}supply chain\b",
    r"\bhead of supply chain\b",
    r"\bdemand plan",              # demand planner, demand planning
    r"\bsupply plan",             # supply planner, supply planning
    r"\bprocurement\b",
    r"\boperations\b",
    r"\blogistics\b",
    r"\bmaterials management\b",
    r"\binventory\b",
    r"\bsupply chain\b",          # generic — lower priority but still relevant
]

# Medium-value: economic buyers and influencers
_TITLE_MEDIUM = [
    r"\bceo\b",
    r"\bchief executive\b",
    r"\bcoo\b",
    r"\bchief operating\b",
    r"\bcio\b",
    r"\bchief information\b",
    r"\bchief technology\b",
    r"\bcto\b",
    r"\bvp.{0,10}operations\b",
    r"\bvice president.{0,10}operations\b",
    r"\bdirector.{0,10}operations\b",
    r"\bmanufacturing\b",
    r"\bplanning\b",
    r"\bforecasting\b",
]

# Compile regexes at module load
_RE_HIGH = [re.compile(p, re.IGNORECASE) for p in _TITLE_HIGH]
_RE_MEDIUM = [re.compile(p, re.IGNORECASE) for p in _TITLE_MEDIUM]


def _score_title_relevance(contact: dict[str, Any]) -> float:
    """Return 0–1 based on title/role match to Eloso's ICP."""
    title = _get_meta(contact, "title") or ""
    notes = contact.get("notes") or ""
    tags = [t.lower() for t in (contact.get("tags") or [])]

    # Also scan tags for role keywords
    text = f"{title} {notes}".strip()
    if not text and not tags:
        return 0.0

    for pat in _RE_HIGH:
        if pat.search(text):
            return 1.0

    # Check tags for supply-chain-relevant keywords
    supply_chain_tags = {"supply-chain", "supply_chain", "scm", "operations", "procurement", "logistics"}
    if supply_chain_tags.intersection(set(tags)):
        return 1.0

    for pat in _RE_MEDIUM:
        if pat.search(text):
            return 0.55

    return 0.0


# ---------------------------------------------------------------------------
# Seniority
# ---------------------------------------------------------------------------

_SENIORITY_CLEVEL = [
    r"\bceo\b", r"\bcoo\b", r"\bcto\b", r"\bcfo\b", r"\bcio\b", r"\bcsco\b",
    r"\bchief\b", r"\bpresident\b", r"\bfounder\b", r"\bco-founder\b",
]
_SENIORITY_VP = [
    r"\bvp\b", r"\bvice president\b", r"\bsvp\b", r"\bevp\b", r"\bgm\b",
    r"\bgeneral manager\b",
]
_SENIORITY_DIR = [
    r"\bdirector\b", r"\bhead of\b", r"\bsenior director\b",
]
_SENIORITY_MGR = [
    r"\bmanager\b", r"\blead\b", r"\bsenior\b", r"\bprincipal\b",
]

_RE_CLEVEL = [re.compile(p, re.IGNORECASE) for p in _SENIORITY_CLEVEL]
_RE_VP = [re.compile(p, re.IGNORECASE) for p in _SENIORITY_VP]
_RE_DIR = [re.compile(p, re.IGNORECASE) for p in _SENIORITY_DIR]
_RE_MGR = [re.compile(p, re.IGNORECASE) for p in _SENIORITY_MGR]


def _score_seniority(contact: dict[str, Any]) -> float:
    """Return 0–1 based on seniority level inferred from title."""
    title = _get_meta(contact, "title") or ""
    if not title:
        return 0.0

    if any(pat.search(title) for pat in _RE_CLEVEL):
        return 1.0
    if any(pat.search(title) for pat in _RE_VP):
        return 0.80
    if any(pat.search(title) for pat in _RE_DIR):
        return 0.65
    if any(pat.search(title) for pat in _RE_MGR):
        return 0.40
    # Individual contributor / staff
    return 0.20


# ---------------------------------------------------------------------------
# Org type
# ---------------------------------------------------------------------------

# Tags that mark prospect orgs — our highest-priority relationship type
_PROSPECT_TAGS = {"prospect", "eloso", "prospect-contact"}
_VC_TAGS = {"vc", "investor", "seed", "series-a", "series-b", "pre-seed"}
_ALLY_TAGS = {"ally", "advisor", "board", "partner"}


def _score_org_type(contact: dict[str, Any]) -> float:
    """
    Score the contact's organizational context.
    Prospect contacts score highest; VCs are medium (they're important but not buyers).
    Allies/advisors are medium-high. Unknown = low.
    """
    tags = {t.lower() for t in (contact.get("tags") or [])}

    # Check org_tags if provided (the org the person works at)
    org_tags = {t.lower() for t in (contact.get("org_tags") or [])}
    combined = tags | org_tags

    if combined & _PROSPECT_TAGS:
        return 1.0
    if combined & _ALLY_TAGS:
        return 0.70
    if combined & _VC_TAGS:
        return 0.45
    return 0.15


# ---------------------------------------------------------------------------
# Interaction recency
# ---------------------------------------------------------------------------

_DAYS_VERY_RECENT = 30     # <30 days → full score
_DAYS_RECENT = 90          # 30–90 → good
_DAYS_STALE = 180          # 90–180 → okay
_DAYS_COLD = 365           # 180–365 → stale
# >365 → very cold


def _score_interaction_recency(contact: dict[str, Any]) -> float:
    """
    Score based on most recent interaction.
    Uses last_interaction_at (ISO string) if provided, else updatedAt as fallback.
    """
    now = datetime.now(tz=timezone.utc)

    # Prefer explicit last interaction date
    last_interaction_str = contact.get("last_interaction_at")
    if last_interaction_str:
        try:
            last = datetime.fromisoformat(last_interaction_str)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            days_ago = (now - last).days
            return _recency_to_score(days_ago)
        except (ValueError, TypeError):
            pass

    # Fall back to updatedAt
    updated_at = contact.get("updatedAt") or contact.get("updated_at")
    if updated_at:
        try:
            last = datetime.fromisoformat(updated_at)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            days_ago = (now - last).days
            # updatedAt is less reliable than an explicit interaction — cap at 0.6
            return min(_recency_to_score(days_ago), 0.6)
        except (ValueError, TypeError):
            pass

    # No date info → unknown recency (penalize)
    return 0.1


def _recency_to_score(days_ago: int) -> float:
    if days_ago <= _DAYS_VERY_RECENT:
        return 1.0
    if days_ago <= _DAYS_RECENT:
        # Linear interpolation 1.0 → 0.75 from 30 → 90 days
        return 1.0 - 0.25 * (days_ago - _DAYS_VERY_RECENT) / (_DAYS_RECENT - _DAYS_VERY_RECENT)
    if days_ago <= _DAYS_STALE:
        # Linear interpolation 0.75 → 0.40 from 90 → 180 days
        return 0.75 - 0.35 * (days_ago - _DAYS_RECENT) / (_DAYS_STALE - _DAYS_RECENT)
    if days_ago <= _DAYS_COLD:
        # Linear interpolation 0.40 → 0.15 from 180 → 365 days
        return 0.40 - 0.25 * (days_ago - _DAYS_STALE) / (_DAYS_COLD - _DAYS_STALE)
    # Over a year old
    return 0.05


# ---------------------------------------------------------------------------
# Network proximity
# ---------------------------------------------------------------------------

_STRONG_RELATIONS = {"ally", "champion", "advisor", "sponsor", "board_member"}
_WARM_RELATIONS = {"works_at", "colleague", "knows", "referred_by", "connected"}


def _score_network_proximity(contact: dict[str, Any]) -> float:
    """
    Score based on edge relationships to allies.
    - edges: list of {relation, strength, target_tags?}
    """
    edges: list[dict] = contact.get("edges") or []
    if not edges:
        return 0.1

    max_score = 0.0
    for edge in edges:
        relation = (edge.get("relation") or "").lower()
        strength = float(edge.get("strength") or 0.0)
        target_tags = {t.lower() for t in (edge.get("target_tags") or [])}

        # Direct ally/champion relationship
        if relation in _STRONG_RELATIONS:
            max_score = max(max_score, min(0.9 + strength * 0.1, 1.0))
        elif relation in _WARM_RELATIONS:
            # Works at a prospect org is a strong proximity signal
            if target_tags & _PROSPECT_TAGS:
                max_score = max(max_score, 0.80 * (0.5 + strength * 0.5))
            else:
                max_score = max(max_score, 0.50 * (0.5 + strength * 0.5))
        elif target_tags & _PROSPECT_TAGS:
            max_score = max(max_score, 0.60)

    return max_score if max_score > 0 else 0.10


# ---------------------------------------------------------------------------
# Record completeness
# ---------------------------------------------------------------------------

_COMPLETENESS_FIELDS = [
    ("email", 0.35),
    ("title", 0.25),
    ("company", 0.20),       # or works_at edge
    ("linkedin_url", 0.10),  # stored as meta key "linkedin_url" or "url"
    ("notes_nonempty", 0.10),
]


def _score_record_completeness(contact: dict[str, Any]) -> float:
    """Return 0–1 based on how complete the contact record is."""
    score = 0.0

    meta = {m["key"]: m["value"] for m in (contact.get("meta") or [])}

    if meta.get("email") or contact.get("email"):
        score += 0.35
    if meta.get("title") or contact.get("title"):
        score += 0.25
    # Company present via meta OR via a works_at edge
    has_company = bool(
        meta.get("company")
        or contact.get("company")
        or any(
            (e.get("relation") or "").lower() == "works_at"
            for e in (contact.get("edges") or [])
        )
    )
    if has_company:
        score += 0.20
    if meta.get("linkedin_url") or meta.get("url") or meta.get("linkedin"):
        score += 0.10
    notes = contact.get("notes") or ""
    if notes.strip():
        score += 0.10

    return min(score, 1.0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_contact(contact: dict[str, Any]) -> dict[str, Any]:
    """
    Compute a 0–100 contact score for Eloso CRM.

    Args:
        contact: A dict with fields from Kissinger (EntityGql) plus optional
                 enrichment fields:
                 - name, kind, tags, notes, meta (list of {key, value})
                 - updatedAt / updated_at
                 - last_interaction_at (ISO string, optional)
                 - edges (list of EdgeGql-like dicts, optional)
                 - org_tags (tags of the org the person works at, optional)

    Returns:
        {
          "score": int (0-100),
          "breakdown": {
            "title_relevance": {"raw": float, "weighted": float, "weight": float},
            "seniority":       {"raw": float, "weighted": float, "weight": float},
            "org_type":        {"raw": float, "weighted": float, "weight": float},
            "interaction_recency": {"raw": float, "weighted": float, "weight": float},
            "network_proximity": {"raw": float, "weighted": float, "weight": float},
            "record_completeness": {"raw": float, "weighted": float, "weight": float},
          }
        }
    """
    factors: dict[str, float] = {
        "title_relevance": _score_title_relevance(contact),
        "seniority": _score_seniority(contact),
        "org_type": _score_org_type(contact),
        "interaction_recency": _score_interaction_recency(contact),
        "network_proximity": _score_network_proximity(contact),
        "record_completeness": _score_record_completeness(contact),
    }

    weighted_sum = sum(factors[k] * WEIGHTS[k] for k in factors)
    score = round(weighted_sum * 100)
    score = max(0, min(100, score))

    breakdown = {
        factor: {
            "raw": round(factors[factor], 3),
            "weight": WEIGHTS[factor],
            "weighted": round(factors[factor] * WEIGHTS[factor], 4),
        }
        for factor in factors
    }

    return {"score": score, "breakdown": breakdown}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _get_meta(contact: dict[str, Any], key: str) -> str | None:
    """Extract a value from the contact's meta list by key."""
    for m in contact.get("meta") or []:
        if isinstance(m, dict) and m.get("key") == key:
            return m.get("value")
    return None
