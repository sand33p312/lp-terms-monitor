"""Validation for candidate obligations extracted from source documents."""
from __future__ import annotations

from typing import Any

REQUIRED_FIELDS = ("investor", "obligation_type", "deliverables", "frequency", "trigger", "source_quote")
ALLOWED_TYPES = {"reporting", "notification", "other"}
ALLOWED_FREQUENCIES = {"monthly", "quarterly", "semi_annual", "annual", "event_driven", "one_time", "unspecified"}
ALLOWED_UNITS = {"calendar_days", "business_days", "unspecified"}


def validate_candidate(candidate: dict[str, Any], source_text: str) -> list[str]:
    """Return validation errors. Empty list means structurally valid, not legally correct."""
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        if field not in candidate or candidate[field] in (None, "", []):
            errors.append(f"Missing required field: {field}")

    if candidate.get("obligation_type") not in ALLOWED_TYPES:
        errors.append("obligation_type must be reporting, notification, or other")

    if candidate.get("frequency") not in ALLOWED_FREQUENCIES:
        errors.append("Unsupported frequency value")

    unit = candidate.get("deadline_unit", "unspecified")
    if unit not in ALLOWED_UNITS:
        errors.append("deadline_unit must be calendar_days, business_days, or unspecified")

    value = candidate.get("deadline_value")
    if value is not None and (not isinstance(value, int) or value < 0):
        errors.append("deadline_value must be a non-negative integer or null")
    if value is None and unit != "unspecified":
        errors.append("A deadline unit cannot be specified when deadline_value is null")
    if value is not None and unit == "unspecified":
        errors.append("A deadline unit is required when deadline_value is provided")

    quote = candidate.get("source_quote", "")
    if quote and quote not in source_text:
        errors.append("source_quote is not an exact substring of the source document")

    if candidate.get("effective_from") and not isinstance(candidate["effective_from"], str):
        errors.append("effective_from must be an ISO date string or null")

    return errors
