import os
import sys
from datetime import date

import pytest

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


# ── locations (2026-10-19): which locations to export ──
from common.vinculum_ui import (  # noqa: E402
    LocationError, is_no_data_message, parse_locations, resolve_locations,
)

AVAIL = ["A02", "A03", "GJ1", "NM4", "S01", "S02"]


def test_blank_or_all_means_every_location_the_user_can_switch_to():
    assert resolve_locations(AVAIL, "") == AVAIL
    assert resolve_locations(AVAIL, "all") == AVAIL
    assert resolve_locations(AVAIL, " * ") == AVAIL


def test_explicit_list_keeps_its_order_and_ignores_case_and_repeats():
    assert resolve_locations(AVAIL, "nm4, A03,nm4") == ["NM4", "A03"]


def test_exclusions():
    assert resolve_locations(AVAIL, "all,-S01,-s02") == ["A02", "A03", "GJ1", "NM4"]
    assert resolve_locations(AVAIL, "-S01") == ["A02", "A03", "GJ1", "NM4", "S02"]


def test_a_location_the_user_cannot_switch_to_is_an_error_not_skipped():
    with pytest.raises(LocationError) as e:
        resolve_locations(AVAIL, "A03,AL4")
    assert "AL4" in str(e.value)


def test_nothing_left_is_an_error():
    with pytest.raises(LocationError):
        resolve_locations(AVAIL, "A03,-A03")
    with pytest.raises(LocationError):
        resolve_locations([], "")


def test_parse_locations():
    d = parse_locations('{"bad": null, "current": "NM4", "options": [["A03", "A03-Kolkata"]]}')
    assert d["current"] == "NM4" and d["options"] == [["A03", "A03-Kolkata"]]
    assert parse_locations("not json")["bad"] == "PARSE"
    assert parse_locations(None)["bad"] == "PARSE"


def test_no_data_messages():
    for text in ("No Record Found", "No data found in the grid", "No records to export",
                 "no result"):
        assert is_no_data_message(text), text
    # anything else must NOT count as "no data" (that location would be skipped)
    for text in ("", None, "Report request submitted successfully", "Session expired",
                 "Report not found", "Print button not found", "0 records", "Notification"):
        assert not is_no_data_message(text), text


# ── export_all_locations: order, switch-back, retry reuse (no browser) ──
def _fake_vinculum(monkeypatch, locs, session):
    import common.vinculum_ui as v
    switches = []
    monkeypatch.setattr(v, "plan_locations", lambda page, base, spec=None: (list(locs), session))
    monkeypatch.setattr(v, "switch_location", lambda page, base, loc, tries=2: switches.append(loc))
    monkeypatch.setattr(v, "session_lost", lambda page: False)
    return v, switches


def test_session_location_first_then_switch_back(monkeypatch, tmp_path):
    v, switches = _fake_vinculum(monkeypatch, ["A03", "NM4", "S01"], "NM4")
    exported = []

    def export_one(loc):
        exported.append(loc)
        if loc == "S01":
            return None
        p = tmp_path / f"{loc}.csv"
        p.write_text("Order No,Source WH\r\nX1," + loc + "\r\n")
        return str(p)

    parts = v.export_all_locations(None, "base", export_one)
    assert exported == ["NM4", "A03", "S01"]          # no switch needed for NM4
    assert switches == ["A03", "S01", "NM4"]          # ... and back to NM4 at the end
    assert [loc for loc, _ in parts] == ["A03", "NM4", "S01"]
    assert parts[2][1] is None


def test_retry_skips_locations_an_earlier_attempt_exported(monkeypatch, tmp_path):
    v, switches = _fake_vinculum(monkeypatch, ["A03", "NM4", "S01"], "NM4")
    done_file = tmp_path / "A03.csv"
    done_file.write_text("Order No\r\nX1\r\n")
    done = {"A03": str(done_file), "S01": None}
    exported = []
    parts = v.export_all_locations(None, "base", lambda loc: exported.append(loc) or None, done=done)
    assert exported == ["NM4"] and switches == []
    assert dict(parts)["A03"] == str(done_file)


def test_a_failed_location_raises_and_keeps_what_was_done(monkeypatch, tmp_path):
    v, switches = _fake_vinculum(monkeypatch, ["A03", "NM4"], "NM4")
    done = {}

    def export_one(loc):
        if loc == "A03":
            raise RuntimeError("report never reached SUCCESS")
        return None

    with pytest.raises(RuntimeError):
        v.export_all_locations(None, "base", export_one, done=done)
    assert done == {"NM4": None}
    assert switches == ["A03", "NM4"]                 # switched back even after the failure


def test_location_spec_env_overrides_the_script_default(monkeypatch):
    from common.vinculum_ui import LOCATIONS_WITH_ORDERS, location_spec
    monkeypatch.delenv("VIN_LOCATIONS", raising=False)
    assert location_spec(LOCATIONS_WITH_ORDERS) == "A03,GJ1,NM1,NM4,NM7,NM9"
    monkeypatch.setenv("VIN_LOCATIONS", "  ")
    assert location_spec("all") == "all"
    monkeypatch.setenv("VIN_LOCATIONS", "all")
    assert location_spec(LOCATIONS_WITH_ORDERS) == "all"
