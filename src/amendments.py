"""Amendment detection, validation, and matching for LP Terms Monitor.

This module identifies explicit contractual change language. It does not make a
legal determination that an amendment is valid, effective, or controlling.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any


INVESTOR_RE = re.compile(r"\b(?:Investor\s+)?(LP[-\s]?[A-Z0-9]+)\b", re.IGNORECASE)
CLAUSE_RE = re.compile(r"\bClause\s+(\d+(?:\.\d+)*)\b", re.IGNORECASE)
DATE_PATTERNS = (
    re.compile(r"\b(?:effective date|with effect from)\s*:?\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4}|\d{4}-\d{2}-\d{2})", re.IGNORECASE),
    re.compile(r"\b(?:effective date|with effect from)\s*:?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})", re.IGNORECASE),
)
DAY_TOKEN_RE = re.compile(
    r"(?P<raw>(?:[A-Za-z]+(?:-[A-Za-z]+)?\s+)?\(?\d{1,3}\)?\s+(?:calendar|business)\s+days)",
    re.IGNORECASE,
)


def detect_document_role(filename: str, text: str, declared_type: str | None = None) -> str:
    """Prefer explicit document metadata; otherwise use heading/name cues conservatively."""
    declared = (declared_type or "").strip().lower()
    if declared in {"lpa", "side_letter", "amendment"}:
        return declared
    name = filename.lower()
    head = text[:1800].lower()
    document_heading = "\n".join(text.splitlines()[:4]).lower()
    if "amendment" in name or re.search(r"\bamendment(?:\s+no\.?|\s+to)\b", document_heading):
        return "amendment"
    if "_lpa" in name or "limited partnership agreement" in head:
        return "lpa"
    if "side letter" in head or "side_letter" in name:
        return "side_letter"
    return "unknown"


def _parse_date(text: str) -> str | None:
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1).replace(",", "").strip()
        for fmt in ("%d %B %Y", "%d %b %Y", "%Y-%m-%d", "%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                continue
    return None


def _day_value(raw: str) -> dict[str, Any] | None:
    match = re.search(r"\(?\s*(\d{1,3})\s*\)?\s+(calendar|business)\s+days", raw, re.IGNORECASE)
    if not match:
        return None
    return {
        "value": int(match.group(1)),
        "unit": f"{match.group(2).lower()}_days",
        "raw": raw.strip(),
    }


def _sentence_containing(text: str, position: int) -> str:
    """Return an exact source substring containing a detected replacement."""
    start = max(text.rfind("\n", 0, position), text.rfind(".", 0, position)) + 1
    endings = [p for p in (text.find(".", position), text.find("\n", position)) if p >= 0]
    end = min(endings) + (1 if endings and text[min(endings)] == "." else 0) if endings else len(text)
    return text[start:end].strip()


def extract_explicit_deadline_amendments(text: str, filename: str = "") -> list[dict[str, Any]]:
    """Deterministically parse narrow, explicit 'old deadline → new deadline' language.

    This fast path deliberately supports only unambiguous numeric day replacements.
    More complex amendment wording should go through the LLM-assisted extractor.
    """
    investor_match = INVESTOR_RE.search(f"{filename}\n{text[:3000]}")
    target_clause_match = re.search(
        r"(?:amendment\s+to|amending|in|of)\s+Clause\s+(\d+(?:\.\d+)*)",
        text,
        re.IGNORECASE,
    )
    if target_clause_match is None:
        all_clause_matches = list(CLAUSE_RE.finditer(text))
        target_clause_match = all_clause_matches[-1] if all_clause_matches else None
    day_matches = list(DAY_TOKEN_RE.finditer(text))
    if not investor_match or not target_clause_match or len(day_matches) < 2:
        return []

    # Only handle explicit replacement/change constructions, not two unrelated day references.
    replacement_cues = re.search(
        r"deleted\s+and\s+replaced\s+with|changed\s+from|amended\s+from|replaced\s+by",
        text,
        re.IGNORECASE,
    )
    if not replacement_cues:
        return []

    cue = replacement_cues.group(0).lower()
    if cue == "changed from":
        old_match = next((m for m in day_matches if m.start() > replacement_cues.end()), None)
        to_match = re.search(r"\bto\b", text[replacement_cues.end():], re.IGNORECASE)
        new_start = replacement_cues.end() + to_match.end() if to_match else replacement_cues.end()
        new_match = next((m for m in day_matches if m.start() >= new_start), None)
    else:
        old_match = next((m for m in day_matches if m.start() < replacement_cues.start()), None)
        new_match = next((m for m in day_matches if m.start() > replacement_cues.end()), None)
    if old_match is None or new_match is None:
        return []
    old_value, new_value = _day_value(old_match.group("raw")), _day_value(new_match.group("raw"))
    if not old_value or not new_value:
        return []

    # Ensure both values occur in the same operative sentence / paragraph.
    paragraph_start = text.rfind("\n", 0, replacement_cues.start()) + 1
    paragraph_end = text.find("\n\n", replacement_cues.start())
    if paragraph_end < 0:
        paragraph_end = len(text)
    paragraph = text[paragraph_start:paragraph_end]
    local_matches = list(DAY_TOKEN_RE.finditer(paragraph))
    if len(local_matches) < 2:
        return []
    quote = paragraph.strip()
    investor_suffix = re.sub(r"^LP[-_\s]*", "", investor_match.group(1), flags=re.IGNORECASE)
    investor = f"LP-{investor_suffix.upper()}"
    clause_number = target_clause_match.group(1)
    effective_date = _parse_date(text)
    if not effective_date:
        return []

    unchanged_match = re.search(r"(All other terms\b.*?remain unchanged\.)", text, re.IGNORECASE | re.DOTALL)
    unchanged_terms = [unchanged_match.group(1).strip()] if unchanged_match else []
    return [{
        "investor": investor,
        "fund": "ABC Private Equity Fund IV" if "ABC PRIVATE EQUITY FUND IV" in text.upper() else None,
        "target_document": f"{investor} Side Letter",
        "target_clause": f"Clause {clause_number}",
        "change_type": "replace_value",
        "field_name": "deadline",
        "old_value": old_value,
        "new_value": new_value,
        "effective_date": effective_date,
        "unchanged_terms": unchanged_terms,
        "source_quote": quote,
        "requires_human_review": True,
        "review_reason": "Mandatory human review: verify amendment authority, effective date, target agreement and clause before changing an obligation.",
        "extraction_method": "deterministic_explicit_replacement_pattern",
        "validation_errors": [],
    }]


def _normalise_investor(value: str | None) -> str:
    if not value:
        return ""
    match = INVESTOR_RE.search(value)
    if not match:
        return value.strip().upper()
    suffix = re.sub(r"^LP[-_\s]*", "", match.group(1), flags=re.IGNORECASE)
    return f"LP-{suffix.upper()}"


def _clause_number(value: str | None) -> str:
    if not value:
        return ""
    if not value:
        return ""
    preferred = re.search(r"(?:amending(?:\s+Side Letter)?|in|of)\s+Clause\s+(\d+(?:\.\d+)*)", value, re.IGNORECASE)
    if preferred:
        return preferred.group(1)
    matches = list(CLAUSE_RE.finditer(value))
    return matches[-1].group(1) if matches else ""


def _deadline_matches(obligation: dict[str, Any], value: dict[str, Any]) -> bool:
    return (
        obligation.get("deadline_value") == value.get("value")
        and obligation.get("deadline_unit") == value.get("unit")
    )


def match_amendment_to_obligations(
    candidate: dict[str, Any], obligations: list[dict[str, Any]]
) -> dict[str, Any]:
    """Match an explicit amendment to an existing obligation without changing it."""
    investor = _normalise_investor(candidate.get("investor"))
    clause = _clause_number(candidate.get("target_clause"))
    old_value = candidate.get("old_value") or {}
    new_value = candidate.get("new_value") or {}
    candidate_fund = (candidate.get("fund") or "").strip().casefold()
    same_scope = [
        o for o in obligations
        if _normalise_investor(o.get("investor")) == investor
        and _clause_number(o.get("clause_reference")) == clause
        and (not candidate_fund or not o.get("fund") or o.get("fund", "").strip().casefold() == candidate_fund)
    ]
    old_matches = [o for o in same_scope if _deadline_matches(o, old_value)]
    new_matches = [o for o in same_scope if _deadline_matches(o, new_value)]

    # Avoid re-applying an amendment already represented in the register.
    applied_new = [
        o for o in new_matches
        if o.get("status") == "approved"
        and o.get("document_role") == "amendment"
        and o.get("effective_from") == candidate.get("effective_date")
    ]
    superseded_old = [o for o in old_matches if o.get("status") == "superseded"]
    active_old = [o for o in old_matches if o.get("status") == "approved"]
    if applied_new and superseded_old:
        return {
            "match_status": "already_applied",
            "matched_obligation_id": superseded_old[0]["id"],
            "active_obligation_id": applied_new[0]["id"],
            "match_note": "The register already contains the old version as superseded and an approved record with the amendment's new value. Do not apply this amendment twice.",
        }

    if applied_new and active_old:
        return {
            "match_status": "conflicting_versions",
            "matched_obligation_id": active_old[0]["id"],
            "active_obligation_id": applied_new[0]["id"],
            "match_note": "An approved amended version exists while the old version is still active. This inconsistent register state must be reconciled manually; no automatic update is allowed.",
        }

    eligible = active_old
    if len(eligible) == 1:
        return {
            "match_status": "matched",
            "matched_obligation_id": eligible[0]["id"],
            "active_obligation_id": None,
            "match_note": f"Matched {investor} {candidate.get('target_clause')} to an approved obligation with the stated old deadline.",
        }
    if len(eligible) > 1:
        return {
            "match_status": "ambiguous",
            "matched_obligation_id": None,
            "active_obligation_id": None,
            "match_note": "Multiple approved obligations match the investor, clause and old deadline. Human must select the intended record.",
        }
    if old_matches:
        return {
            "match_status": "source_not_approved",
            "matched_obligation_id": old_matches[0]["id"],
            "active_obligation_id": None,
            "match_note": "A matching old version exists but is not currently approved. Resolve its status before applying an amendment.",
        }
    return {
        "match_status": "no_match",
        "matched_obligation_id": None,
        "active_obligation_id": None,
        "match_note": "No existing obligation matches the investor, target clause and old value. Do not create or supersede an obligation automatically.",
    }
