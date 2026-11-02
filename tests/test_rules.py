from datetime import date
import pytest

from src.rules import calculate_deadline, days_until, status_for_deadline
from src.validation import validate_candidate


def test_calendar_day_deadline():
    assert calculate_deadline(date(2027, 3, 31), 45, "calendar_days") == date(2027, 5, 15)


def test_business_day_deadline_skips_weekends():
    # 10 business days after Friday 2027-01-01 (the function does not apply holidays).
    assert calculate_deadline(date(2027, 1, 1), 10, "business_days") == date(2027, 1, 15)


def test_zero_day_deadline():
    assert calculate_deadline(date(2027, 3, 31), 0, "calendar_days") == date(2027, 3, 31)


def test_negative_days_rejected():
    with pytest.raises(ValueError):
        calculate_deadline(date(2027, 3, 31), -1, "calendar_days")


def test_unknown_unit_rejected():
    with pytest.raises(ValueError):
        calculate_deadline(date(2027, 3, 31), 5, "weeks")


def test_deadline_status():
    today = date(2027, 5, 1)
    assert status_for_deadline(date(2027, 5, 1), today) == "Due today"
    assert status_for_deadline(date(2027, 5, 5), today) == "Due soon"
    assert status_for_deadline(date(2027, 4, 30), today) == "Overdue"


def test_source_quote_must_be_verbatim():
    candidate = {
        "investor": "LP-A", "obligation_type": "reporting", "deliverables": ["Report"],
        "frequency": "quarterly", "trigger": "quarter end", "deadline_value": 45,
        "deadline_unit": "calendar_days", "source_quote": "This wording is not present.",
    }
    errors = validate_candidate(candidate, "The General Partner shall provide a report.")
    assert "source_quote is not an exact substring of the source document" in errors


def test_ambiguous_deadline_is_structurally_valid():
    candidate = {
        "investor": "LP-C", "obligation_type": "reporting", "deliverables": ["Portfolio data"],
        "frequency": "quarterly", "trigger": "quarter-end", "deadline_value": None,
        "deadline_unit": "unspecified", "source_quote": "as soon as practicable",
    }
    assert validate_candidate(candidate, "Provide information as soon as practicable") == []
