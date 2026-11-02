from datetime import date

from src.consistency import (
    check_amendment_consistency, check_obligation_consistency, day_periods_in, deadline_support, words_to_int,
)
from src.conflicts import find_precedence_questions
from src.rules import calculate_deadline


def test_number_words():
    assert words_to_int("fifteen") == 15
    assert words_to_int("forty-five") == 45
    assert words_to_int("one hundred and twenty") == 120
    assert words_to_int("banana") is None


def test_day_periods_digits_and_words():
    periods = day_periods_in("within forty-five (45) calendar days and fifteen Business Days, or 30 days")
    assert {(p["value"], p["unit"]) for p in periods} == {(45, "calendar_days"), (15, "business_days"), (30, "unstated")}


def test_deadline_support_classes():
    quote = "within forty-five (45) calendar days following the end of each fiscal quarter"
    assert deadline_support(45, "calendar_days", quote) == "supported"
    assert deadline_support(30, "calendar_days", quote) == "value_not_in_quote"
    assert deadline_support(45, "business_days", quote) == "unit_mismatch"
    assert deadline_support(30, "calendar_days", "within thirty days") == "unit_not_stated"
    assert deadline_support(None, "unspecified", quote) == "no_deadline"


def test_invented_deadline_for_soft_clause_is_escalated():
    candidate = {
        "investor": "LP-C", "deadline_value": 30, "deadline_unit": "calendar_days",
        "source_quote": "The General Partner shall use commercially reasonable efforts to provide Investor LP-C "
                        "with additional portfolio information as soon as practicable following the end of each fiscal quarter.",
    }
    warnings = check_obligation_consistency(candidate)
    assert any("not stated in the cited source quote" in w for w in warnings)


def test_missed_deadline_is_flagged():
    candidate = {"investor": "LP-A", "deadline_value": None, "deadline_unit": "unspecified",
                 "source_quote": "Investor LP-A within forty-five (45) calendar days"}
    assert any("no deadline was extracted" in w for w in check_obligation_consistency(candidate))


def test_computed_amendment_value_is_blocked():
    candidate = {
        "old_value": {"value": 75, "unit": "calendar_days"}, "new_value": {"value": 90, "unit": "calendar_days"},
        "source_quote": "the period under Clause 3.1 is extended by a further fifteen (15) calendar days.",
    }
    errors = check_amendment_consistency(candidate)
    assert len(errors) == 2


def test_business_days_respect_holiday_calendar():
    period_end = date(2026, 12, 31)
    assert calculate_deadline(period_end, 10, "business_days") == date(2027, 1, 14)
    assert calculate_deadline(period_end, 10, "business_days", holidays={date(2027, 1, 1)}) == date(2027, 1, 15)


def test_precedence_flag_only_for_same_deliverable_different_deadline():
    base = {"id": "lpa", "investor": "All Limited Partners", "fund": "F", "status": "approved", "document_role": "lpa",
            "deliverables": ["Unaudited quarterly financial statements"], "deadline_value": 60, "deadline_unit": "calendar_days"}
    longer = {**base, "id": "i", "investor": "LP-I", "document_role": "side_letter", "deadline_value": 90}
    same = {**base, "id": "x", "investor": "LP-X", "document_role": "side_letter"}
    other = {**longer, "id": "b", "investor": "LP-B", "deliverables": ["Quarterly ESG report"]}
    superseded = {**longer, "id": "s", "investor": "LP-S", "status": "superseded"}
    flags = find_precedence_questions([base, longer, same, other, superseded])
    assert [f["investor"] for f in flags] == ["LP-I"]
