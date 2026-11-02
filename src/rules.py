"""Deterministic business rules for LP Terms Monitor.

The prototype deliberately supports only simple, explicit deadline expressions.
It does not interpret legal language or resolve conflicting contract provisions.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Optional


def calculate_deadline(
    period_end: date,
    deadline_value: int,
    deadline_unit: str = "calendar_days",
    holidays: Optional[Iterable[date]] = None,
) -> date:
    """Calculate a deadline from a period end.

    Supported units:
      - calendar_days: adds N calendar days to period_end.
      - business_days: adds N weekdays, excluding Saturday/Sunday and any
        dates passed in ``holidays``.

    Important: with no ``holidays`` supplied, business days skip weekends only.
    The caller must supply the holiday calendar the contract's "Business Day"
    definition names (e.g. New York bank holidays); the prototype does not
    guess one. Cut-off times and time zones are not modelled.
    """
    if deadline_value < 0:
        raise ValueError("deadline_value must be zero or greater")
    normalized = deadline_unit.strip().lower()
    if normalized == "calendar_days":
        return period_end + timedelta(days=deadline_value)
    if normalized == "business_days":
        closed = set(holidays or ())
        result = period_end
        added = 0
        while added < deadline_value:
            result += timedelta(days=1)
            if result.weekday() < 5 and result not in closed:
                added += 1
        return result
    raise ValueError(f"Unsupported deadline unit: {deadline_unit}")


def days_until(deadline: date, today: Optional[date] = None) -> int:
    """Return positive days remaining, zero if due today, negative if overdue."""
    today = today or date.today()
    return (deadline - today).days


def status_for_deadline(deadline: date, today: Optional[date] = None) -> str:
    """A simple display status; fulfilment state is stored separately."""
    remaining = days_until(deadline, today)
    if remaining < 0:
        return "Overdue"
    if remaining == 0:
        return "Due today"
    if remaining <= 7:
        return "Due soon"
    return "Upcoming"
