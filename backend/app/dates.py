"""Date helpers shared across modules that need to reason about a
customer's monthly credit ("payday") cycle. Extracted here (2026-09-03,
Phase 5) so generator/oracle.py and executor/executor.py -- which both
need "the next occurrence of day-of-month X on or after date Y" -- share
one implementation instead of two copies drifting apart.
"""

import calendar
from datetime import date


def next_payday_on_or_after(start: date, typical_credit_day: int) -> date:
    """The next date on/after `start` whose day-of-month is
    `typical_credit_day`, clamped to each month's actual length (handles
    e.g. typical_credit_day=28 rolling correctly through February)."""
    day = min(typical_credit_day, calendar.monthrange(start.year, start.month)[1])
    candidate = date(start.year, start.month, day)
    if candidate >= start:
        return candidate
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    day = min(typical_credit_day, calendar.monthrange(year, month)[1])
    return date(year, month, day)
