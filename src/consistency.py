"""Deterministic value-to-quote consistency checks.

Quote grounding (quote_grounding.py) proves that a cited quote exists in the
source. It does not prove that the *values* the model extracted are stated in
that quote. A model can cite the correct clause and still invent a deadline.

These checks close that gap without any model call: the extracted number and
unit must be literally readable from the cited quote, either as digits
("(45) calendar days") or as English number words ("fifteen Business Days").
Nothing here interprets legal meaning; a pass means "the number is written in
the cited text", not "the obligation is correctly understood".
"""
from __future__ import annotations

import re
from typing import Any

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUMBER_WORD = "|".join(sorted([*_UNITS, *_TENS, "hundred", "and"], key=len, reverse=True))
_DAY_PERIOD_RE = re.compile(
    rf"(?P<words>(?:\b(?:{_NUMBER_WORD})[\s-]+)*)"   # optional number words
    r"(?:\(\s*(?P<paren>\d{1,4})\s*\)\s*|(?P<digits>\b\d{1,4})\s+)?"  # optional (45) or 45
    r"(?P<unit>calendar\s+days|business\s+days|days)\b",
    re.IGNORECASE,
)


def words_to_int(text: str) -> int | None:
    """Convert English number words up to 9,999 ('one hundred and twenty') to int."""
    tokens = [t for t in re.split(r"[\s-]+", (text or "").strip().lower()) if t and t != "and"]
    if not tokens:
        return None
    total = 0
    current = 0
    for token in tokens:
        if token in _UNITS:
            current += _UNITS[token]
        elif token in _TENS:
            current += _TENS[token]
        elif token == "hundred":
            current = (current or 1) * 100
        else:
            return None
    return total + current


def day_periods_in(text: str) -> list[dict[str, Any]]:
    """Return every day period literally stated in text, with value and unit.

    unit is 'calendar_days', 'business_days' or 'unstated' (the text says only
    'days'). value is None when no number is attached to the period.
    """
    periods = []
    for match in _DAY_PERIOD_RE.finditer(text or ""):
        value = None
        if match.group("paren"):
            value = int(match.group("paren"))
        elif match.group("digits"):
            value = int(match.group("digits"))
        elif match.group("words").strip():
            value = words_to_int(match.group("words"))
        unit_raw = re.sub(r"\s+", " ", match.group("unit").lower())
        unit = {"calendar days": "calendar_days", "business days": "business_days"}.get(unit_raw, "unstated")
        if value is None:
            continue
        periods.append({"value": value, "unit": unit, "raw": match.group(0).strip()})
    return periods


def deadline_support(value: Any, unit: Any, quote: str) -> str:
    """Classify whether a (value, unit) pair is literally stated in quote.

    Returns one of: 'supported', 'unit_not_stated', 'unit_mismatch',
    'value_not_in_quote', 'no_deadline'.
    """
    if value is None:
        return "no_deadline"
    periods = day_periods_in(quote)
    same_value = [p for p in periods if p["value"] == value]
    if not same_value:
        return "value_not_in_quote"
    if any(p["unit"] == unit for p in same_value):
        return "supported"
    if any(p["unit"] == "unstated" for p in same_value):
        return "unit_not_stated"
    return "unit_mismatch"


_SUPPORT_MESSAGES = {
    "value_not_in_quote": "deadline {value} {unit} is not stated in the cited source quote (possible invented or computed deadline)",
    "unit_not_stated": "the cited quote says '{value} days' without 'calendar' or 'business'; the unit was assumed, not read",
    "unit_mismatch": "deadline unit {unit} contradicts the unit written in the cited source quote",
}


def check_obligation_consistency(candidate: dict[str, Any]) -> list[str]:
    """Warnings that force an obligation candidate into escalated human review."""
    warnings: list[str] = []
    quote = candidate.get("source_quote") or ""
    value = candidate.get("deadline_value")
    unit = candidate.get("deadline_unit")
    status = deadline_support(value, unit, quote)
    if status in _SUPPORT_MESSAGES:
        warnings.append(_SUPPORT_MESSAGES[status].format(value=value, unit=unit))
    if value is None and day_periods_in(quote):
        stated = ", ".join(p["raw"] for p in day_periods_in(quote))
        warnings.append(f"no deadline was extracted but the cited quote states a day period ({stated})")
    investor = (candidate.get("investor") or "").strip()
    if investor and re.match(r"^LP-[A-Z0-9]+$", investor) and investor.casefold() not in quote.casefold():
        warnings.append(f"investor {investor} is not named in the cited quote; confirm the clause applies to this investor")
    return warnings


def check_amendment_consistency(candidate: dict[str, Any]) -> list[str]:
    """Errors that block saving an amendment candidate.

    Amendments change approved records, so a value that cannot be read from the
    cited amendment text is treated as a hard error rather than a warning.
    """
    errors: list[str] = []
    quote = candidate.get("source_quote") or ""
    for key in ("old_value", "new_value"):
        item = candidate.get(key)
        if not isinstance(item, dict):
            continue
        status = deadline_support(item.get("value"), item.get("unit"), quote)
        if status in _SUPPORT_MESSAGES:
            errors.append(f"{key}: " + _SUPPORT_MESSAGES[status].format(value=item.get("value"), unit=item.get("unit")))
    old, new = candidate.get("old_value"), candidate.get("new_value")
    if isinstance(old, dict) and isinstance(new, dict) and old.get("value") == new.get("value") and old.get("unit") == new.get("unit"):
        errors.append("old_value and new_value are identical; nothing to apply")
    return errors


def amendment_review_notes(candidate: dict[str, Any]) -> list[str]:
    """Non-blocking notes a reviewer should see before approving."""
    notes = []
    old, new = candidate.get("old_value"), candidate.get("new_value")
    if not (isinstance(old, dict) and isinstance(new, dict)):
        return notes
    if old.get("unit") and new.get("unit") and old.get("unit") != new.get("unit"):
        notes.append(f"counting convention changes from {old['unit']} to {new['unit']}; check holiday calendar and downstream schedules")
    return notes
