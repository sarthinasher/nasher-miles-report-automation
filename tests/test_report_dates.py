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


def test_default_window_is_last_seven_days_oldest_first():
    # 7 since 2026-10-13 (was 3): a multi-day Vinculum outage is back-filled by itself.
    assert default_window(TODAY) == [date(2026, 10, d) for d in range(3, 10)]
    assert default_window(TODAY, 3) == [date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)]
    assert default_window(TODAY, 1) == [date(2026, 10, 9)]


def test_filename_uses_report_date_not_run_date():
    assert drive_filename(date(2026, 10, 6)) == "OrderEnquiry_06-10-2026"
    assert vinculum_date(date(2026, 10, 6)) == "06/10/2026"


def test_pending_skips_existing_files():
    days = default_window(TODAY, 3)
    existing = {"OrderEnquiry_07-10-2026", "OrderEnquiry_05-10-2026"}
    assert pending_dates(days, existing) == [date(2026, 10, 8), date(2026, 10, 9)]


def test_should_retry():
    assert should_retry(1, 2) is True
    assert should_retry(2, 2) is False
    assert should_retry(1, 2, error_retryable=False) is False


# ── 2026-10-13: catch-up for Pick Pack / Sales Report (same rules as Order Enquiry) ──
from common.report_dates import (  # noqa: E402
    ORDER_ENQUIRY_NAME, PICK_PACK_NAME, SALES_REPORT_NAME, dates_to_export,
)

DAY13 = date(2026, 10, 13)


def test_file_names_match_what_consumers_read():
    d = date(2026, 9, 27)
    assert drive_filename(d, *PICK_PACK_NAME) == "PickPackReport_27.09.2026"
    assert drive_filename(d, *SALES_REPORT_NAME) == "27.09.2026"
    assert drive_filename(d, *ORDER_ENQUIRY_NAME) == "OrderEnquiry_27-09-2026"


def test_catch_up_exports_only_missing_days_of_last_week():
    # Pick Pack folder on 13-Oct: everything up to 10.10 present, login broke on 12-Oct.
    existing = {f"PickPackReport_{d:02d}.10.2026" for d in range(1, 11)}
    days, mode = dates_to_export("", DAY13, existing, PICK_PACK_NAME)
    assert days == [date(2026, 10, 11), date(2026, 10, 12)]
    assert "catch-up" in mode


def test_catch_up_without_listing_exports_whole_window():
    days, _ = dates_to_export("", DAY13, None, SALES_REPORT_NAME, lookback_days=3)
    assert days == [date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12)]


def test_requested_backfill_reexports_unless_skip_existing():
    existing = {"PickPackReport_27.09.2026"}
    days, mode = dates_to_export("27/09/2026,11/10/2026-12/10/2026", DAY13, existing,
                                 PICK_PACK_NAME)
    assert days == [date(2026, 9, 27), date(2026, 10, 11), date(2026, 10, 12)]
    days, _ = dates_to_export("27/09/2026", DAY13, existing, PICK_PACK_NAME,
                              skip_existing_requested=True)
    assert days == []


def test_requested_today_is_rejected():
    with pytest.raises(ReportDateError):
        dates_to_export("13/10/2026", DAY13, set(), SALES_REPORT_NAME)
