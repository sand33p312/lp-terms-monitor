"""Deterministic field-level evidence lookup for extracted contract fields.

The helper never invents evidence: every returned quote is an exact substring
of the stored source document. A field is marked not_found when its extracted
term cannot be located literally in the source text.
"""
from __future__ import annotations

import re
from typing import Any

CLAUSE_HEADING_RE = re.compile(
    r"(?im)^\s*(?:Clause|Section)\s+(\d+(?:\.\d+)*)\s*[—–:-]?[^\n]*$"
)
SENTENCE_RE = re.compile(r"[^\n]+?(?:[.!?](?=\s|$)|$)")


def _clause_at(text: str, position: int) -> str | None:
    current = None
    for match in CLAUSE_HEADING_RE.finditer(text):
        if match.start() > position:
            break
        current = match.group(1)
    return f"Clause {current}" if current else None


def _sentence_span(text: str, position: int) -> tuple[int, int]:
    line_start = text.rfind("\n", 0, position) + 1
    line_end = text.find("\n", position)
    if line_end < 0:
        line_end = len(text)
    line = text[line_start:line_end]
    # If the line is part of a paragraph, preserve the sentence rather than
    # returning the complete paragraph. Offsets are calculated on exact text.
    for match in SENTENCE_RE.finditer(line):
        absolute_start = line_start + match.start()
        absolute_end = line_start + match.end()
        if absolute_start <= position < absolute_end:
            return absolute_start, absolute_end
    return line_start, line_end


def _evidence_for_term(text: str, term: str, field: str) -> dict[str, Any]:
    term = (term or "").strip()
    if not term:
        return {"field": field, "status": "not_found", "quote": None, "clause": None}
    matches = list(re.finditer(re.escape(term), text, re.IGNORECASE))
    if not matches:
        return {"field": field, "status": "not_found", "quote": None, "clause": None,
                "searched_term": term}
    # Prefer the operative sentence (e.g. a sentence containing "shall provide")
    # over a clause heading that happens to repeat the same word.
    scored = []
    for match in matches:
        start, end = _sentence_span(text, match.start())
        quote_candidate = text[start:end].strip()
        lower_quote = quote_candidate.casefold()
        score = sum(token in lower_quote for token in ("shall", "must", "will", "provide", "deliver", "include", "notify"))
        scored.append((score, -match.start(), match, start, end))
    _, _, match, start, end = max(scored, key=lambda item: (item[0], item[1]))
    quote = text[start:end].strip()
    return {
        "field": field,
        "status": "verified_substring",
        "quote": quote,
        "clause": _clause_at(text, match.start()),
        "matched_text": text[match.start():match.end()],
        "quote_is_exact_source_substring": quote in text,
    }


def _deadline_term(candidate: dict[str, Any]) -> str:
    value = candidate.get("deadline_value")
    unit = candidate.get("deadline_unit")
    if value is None or unit not in {"calendar_days", "business_days"}:
        return ""
    label = "calendar" if unit == "calendar_days" else "business"
    # Match a numeric deadline token; the containing quote captures words and
    # numeric notation exactly as they appear in the agreement.
    match = re.search(
        rf"(?i)(?:\b\w+\s+)?\(?\s*{int(value)}\s*\)?\s+{label}\s+days\b",
        candidate.get("_source_text", ""),
    )
    if match:
        return match.group(0)
    return f"{int(value)} {label} days"


def build_field_evidence(source_text: str, candidate: dict[str, Any]) -> dict[str, Any]:
    """Build field-level evidence for an obligation candidate.

    Exact source phrases are located for each field. The returned `summary`
    distinguishes complete literal traceability from partial traceability.
    """
    working = {**candidate, "_source_text": source_text}
    fields: dict[str, list[dict[str, Any]]] = {}

    fields["investor"] = [_evidence_for_term(source_text, str(candidate.get("investor") or ""), "investor")]
    fields["fund"] = [_evidence_for_term(source_text, str(candidate.get("fund") or ""), "fund")]

    deliverable_evidence = [
        _evidence_for_term(source_text, str(item), f"deliverables[{index}]")
        for index, item in enumerate(candidate.get("deliverables") or [])
    ]
    fields["deliverables"] = deliverable_evidence

    freq = str(candidate.get("frequency") or "").strip()
    frequency_evidence = _evidence_for_term(source_text, freq, "frequency")
    if frequency_evidence["status"] == "not_found" and candidate.get("trigger"):
        trigger_evidence = _evidence_for_term(source_text, str(candidate.get("trigger")), "frequency")
        trigger_quote = trigger_evidence.get("quote") or ""
        trigger_lower = trigger_quote.casefold()
        if freq.casefold() == "quarterly" and "quarter" in trigger_lower:
            frequency_evidence = {**trigger_evidence, "status": "inferred_from_trigger",
                                  "inference": "Frequency is inferred from a recurring fiscal-quarter trigger; the word 'quarterly' is not stated literally."}
        elif freq.casefold() == "monthly" and "month" in trigger_lower:
            frequency_evidence = {**trigger_evidence, "status": "inferred_from_trigger",
                                  "inference": "Frequency is inferred from a recurring monthly trigger; the word 'monthly' is not stated literally."}
    fields["frequency"] = [frequency_evidence]

    deadline_term = _deadline_term(working)
    deadline_evidence = _evidence_for_term(source_text, deadline_term, "deadline")
    if candidate.get("deadline_value") is None or candidate.get("deadline_unit") == "unspecified":
        trigger_evidence = _evidence_for_term(source_text, str(candidate.get("trigger") or ""), "deadline")
        trigger_quote = trigger_evidence.get("quote") or ""
        has_numeric_day_period = bool(re.search(r"\b\d+\s+(?:calendar|business)\s+days\b", trigger_quote, re.IGNORECASE))
        if trigger_quote and not has_numeric_day_period:
            deadline_evidence = {
                **trigger_evidence,
                "field": "deadline",
                "status": "not_explicitly_specified",
                "interpretation": "The cited operative wording contains no numeric calendar-day or business-day deadline. Do not calculate a due date from this text alone.",
            }
    fields["deadline"] = [deadline_evidence]

    trigger = str(candidate.get("trigger") or "").strip()
    fields["trigger"] = [_evidence_for_term(source_text, trigger, "trigger")]

    responsible = str(candidate.get("responsible_party") or "").strip()
    fields["responsible_party"] = [_evidence_for_term(source_text, responsible, "responsible_party")]

    fields["conditions"] = [
        _evidence_for_term(source_text, str(item), f"conditions[{index}]")
        for index, item in enumerate(candidate.get("conditions") or [])
    ]

    if candidate.get("clause_reference"):
        clause_number = re.search(r"(\d+(?:\.\d+)*)", str(candidate["clause_reference"]))
        if clause_number:
            heading_match = re.search(
                rf"(?im)^\s*(?:Clause|Section)\s+{re.escape(clause_number.group(1))}\b[^\n]*$",
                source_text,
            )
            fields["clause_reference"] = [{
                "field": "clause_reference",
                "status": "verified_substring" if heading_match else "not_found",
                "quote": heading_match.group(0).strip() if heading_match else None,
                "clause": f"Clause {clause_number.group(1)}" if heading_match else None,
                "quote_is_exact_source_substring": bool(heading_match and heading_match.group(0).strip() in source_text),
            }]

    all_evidence = [item for items in fields.values() for item in items]
    found = sum(item.get("status") == "verified_substring" for item in all_evidence)
    inferred = sum(item.get("status") == "inferred_from_trigger" for item in all_evidence)
    explicitly_unspecified = sum(item.get("status") == "not_explicitly_specified" for item in all_evidence)
    missing = len(all_evidence) - found - inferred - explicitly_unspecified
    return {
        "fields": fields,
        "verified_field_evidence_count": found,
        "inferred_field_count": inferred,
        "explicitly_unspecified_field_count": explicitly_unspecified,
        "unlocated_field_count": missing,
        "summary": "complete_field_traceability" if missing == 0 else "partial_field_traceability",
        "note": "Literal matches, trigger-based inferences and explicitly unspecified fields are distinguished. Evidence lookup does not prove legal interpretation.",
    }


def build_amendment_evidence(source_text: str, candidate: dict[str, Any]) -> dict[str, Any]:
    """Return field-level source evidence for an amendment proposal."""
    fields: dict[str, list[dict[str, Any]]] = {}
    fields["investor"] = [_evidence_for_term(source_text, str(candidate.get("investor") or ""), "investor")]
    clause = str(candidate.get("target_clause") or "")
    clause_num = re.search(r"(\d+(?:\.\d+)*)", clause)
    clause_match = None
    if clause_num:
        clause_match = re.search(
            rf"(?i)\bClause\s+{re.escape(clause_num.group(1))}\b", source_text
        )
    fields["target_clause"] = [{
        "field": "target_clause", "status": "verified_substring" if clause_match else "not_found",
        "quote": clause_match.group(0) if clause_match else None,
        "clause": None,
        "quote_is_exact_source_substring": bool(clause_match and clause_match.group(0) in source_text),
    }]
    for name in ("old_value", "new_value"):
        value = candidate.get(name) or {}
        raw = str(value.get("raw") or "")
        fields[name] = [_evidence_for_term(source_text, raw, name)]
    effective = str(candidate.get("effective_date") or "")
    if effective:
        # Accept ISO or common written date as evidence; exact text is returned.
        date_match = re.search(re.escape(effective), source_text, re.IGNORECASE)
        if not date_match:
            try:
                from datetime import date
                parsed = date.fromisoformat(effective)
                written = f"{parsed.day} {parsed.strftime('%B %Y')}"
                date_match = re.search(re.escape(written), source_text, re.IGNORECASE)
            except (ValueError, OSError):
                date_match = None
        if date_match:
            start, end = _sentence_span(source_text, date_match.start())
            date_quote = source_text[start:end].strip()
            fields["effective_date"] = [{"field": "effective_date", "status": "verified_substring",
                                          "quote": date_quote, "clause": None,
                                          "quote_is_exact_source_substring": date_quote in source_text}]
        else:
            fields["effective_date"] = [{"field": "effective_date", "status": "not_found", "quote": None, "clause": None}]
    all_items = [item for values in fields.values() for item in values]
    found = sum(item.get("status") == "verified_substring" for item in all_items)
    return {"fields": fields, "verified_field_evidence_count": found,
            "unlocated_field_count": len(all_items) - found,
            "summary": "complete_literal_traceability" if found == len(all_items) else "partial_literal_traceability",
            "note": "Exact text matching supports review; it does not establish legal validity or precedence."}
