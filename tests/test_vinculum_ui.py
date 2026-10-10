import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.report_dates import drive_filename, week_start, weekly_file_present  # noqa: E402
from common.vinculum_ui import all_fields_selected, parse_field_state  # noqa: E402

SKU = "SKU Master "


# ── all_fields_selected: the "never export a partial selection" guard ──
def test_all_selected_with_jqgrid_agreeing():
    assert all_fields_selected({"total": 39, "checked": 39, "jqSelected": 39, "bad": None})


def test_all_selected_without_jqgrid_info():
    # grid not a jqGrid multiselect -> only the checkbox count is checked
    assert all_fields_selected({"total": 48, "checked": 48, "jqSelected": None, "bad": None})


def test_the_3_of_39_failure_is_rejected():
    assert not all_fields_selected({"total": 39, "checked": 3, "jqSelected": 3, "bad": None})


def test_checkboxes_full_but_jqgrid_disagrees_is_rejected():
    assert not all_fields_selected({"total": 39, "checked": 39, "jqSelected": 38, "bad": None})


def test_no_grid_or_empty_grid_is_rejected():
    assert not all_fields_selected({"total": 0, "checked": 0, "bad": "NO_GRID"})
    assert not all_fields_selected({"total": 0, "checked": 0, "bad": None})


def test_parse_field_state_bad_input_is_never_selected():
    for raw in (None, "", "not json", "[1,2]"):
        st = parse_field_state(raw)
        assert st["bad"] == "PARSE"
        assert not all_fields_selected(st)


def test_parse_field_state_ok():
    st = parse_field_state('{"total":48,"checked":48,"unchecked":[],"jqSelected":48,"jqRows":48,"bad":null}')
    assert all_fields_selected(st)


# ── weekly catch-up for the SKU Master snapshot ──
def test_week_start_is_monday():
    assert week_start(date(2026, 10, 5)) == date(2026, 10, 5)     # Monday
    assert week_start(date(2026, 10, 6)) == date(2026, 10, 5)     # Tuesday
    assert week_start(date(2026, 10, 11)) == date(2026, 10, 5)    # Sunday


def test_sku_filename_format_unchanged():
    assert drive_filename(date(2026, 10, 5), SKU) == "SKU Master 05-10-2026"


def test_monday_file_means_later_runs_skip():
    names = {"SKU Master 05-10-2026", "SKU Master 28-09-2026"}
    assert weekly_file_present(date(2026, 10, 5), names, SKU)     # Mon 2nd run
    assert weekly_file_present(date(2026, 10, 6), names, SKU)     # Tue catch-up


def test_failed_monday_means_tuesday_exports():
    names = {"SKU Master 28-09-2026"}                              # last week only
    assert not weekly_file_present(date(2026, 10, 5), names, SKU)
    assert not weekly_file_present(date(2026, 10, 6), names, SKU)


def test_manual_mid_week_file_counts_for_the_week():
    names = {"SKU Master 10-10-2026"}                              # Saturday manual run
    assert weekly_file_present(date(2026, 10, 10), names, SKU)
    assert not weekly_file_present(date(2026, 10, 12), names, SKU)  # next Monday: new week


def test_future_dated_file_in_week_is_ignored():
    # only Monday..today counts
    assert not weekly_file_present(date(2026, 10, 6), {"SKU Master 08-10-2026"}, SKU)
