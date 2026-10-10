import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.report_dates import (  # noqa: E402
    ReportDateError, default_window, drive_filename, parse_report_dates,
    pending_dates, should_retry, vinculum_date,
)

TODAY = date(2026, 10, 10)


def test_blank_input_means_default_window():
    assert parse_report_dates("", TODAY) == []
    assert parse_report_dates(None, TODAY) == []
    assert parse_report_dates("   ", TODAY) == []


def test_single_date():
    assert parse_report_dates("08/10/2026", TODAY) == [date(2026, 10, 8)]


def test_list_is_sorted_and_deduplicated():
    got = parse_report_dates("08/10/2026, 06/10/2026,08/10/2026", TODAY)
    assert got == [date(2026, 10, 6), date(2026, 10, 8)]


def test_inclusive_range():
    got = parse_report_dates("06/10/2026-09/10/2026", TODAY)
    assert got == [date(2026, 10, d) for d in (6, 7, 8, 9)]


def test_range_across_month_end():
    got = parse_report_dates("30/09/2026-02/10/2026", TODAY)
    assert got == [date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2)]


@pytest.mark.parametrize("bad", [
    "2026-10-08",            # ISO, not DD/MM/YYYY
    "10/13/2026",            # month 13
    "8-10-2026",             # dashes read as a range of two invalid dates
    "09/10/2026-06/10/2026", # backwards range
    "01/01/2026-01/03/2026", # > 31 days
    "10/10/2026",            # today: day not complete yet
    "11/10/2026",            # future
    ",",
])
def test_rejects_bad_input(bad):
    with pytest.raises(ReportDateError):
        parse_report_dates(bad, TODAY)


def test_default_window_is_last_three_days_oldest_first():
    assert default_window(TODAY) == [date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)]
    assert default_window(TODAY, 1) == [date(2026, 10, 9)]


def test_filename_uses_report_date_not_run_date():
    assert drive_filename(date(2026, 10, 6)) == "OrderEnquiry_06-10-2026"
    assert vinculum_date(date(2026, 10, 6)) == "06/10/2026"


def test_pending_skips_existing_files():
    days = default_window(TODAY)
    existing = {"OrderEnquiry_07-10-2026", "OrderEnquiry_05-10-2026"}
    assert pending_dates(days, existing) == [date(2026, 10, 8), date(2026, 10, 9)]


def test_should_retry():
    assert should_retry(1, 2) is True
    assert should_retry(2, 2) is False
    assert should_retry(1, 2, error_retryable=False) is False
