import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.report_merge import (  # noqa: E402
    MergeError, count_rows, merge_location_files, merge_tables, read_csv_rows, read_xlsx_rows,
)

PP_HEADER = ["Order No", "SKU Code", "Qty", "Source WH"]


# ── merge_tables ─────────────────────────────────────────────
def test_rows_of_every_location_are_kept_with_the_first_header():
    header, rows, counts, dropped = merge_tables([
        ("NM4", [PP_HEADER, ["M1", "A", "1", "NM4"], ["M2", "B", "2", "NM4"]]),
        ("A03", [PP_HEADER, ["O1", "C", "1", "A03"]]),
    ])
    assert header == PP_HEADER
    assert rows == [["M1", "A", "1", "NM4"], ["M2", "B", "2", "NM4"], ["O1", "C", "1", "A03"]]
    assert counts == {"NM4": 2, "A03": 1}
    assert dropped == 0


def test_columns_are_matched_by_name_not_position():
    other = ["Source WH", "Order No", "SKU Code", "Qty"]
    header, rows, _, _ = merge_tables([
        ("NM4", [PP_HEADER, ["M1", "A", "1", "NM4"]]),
        ("A03", [other, ["A03", "O1", "C", "5"]]),
    ])
    assert header == PP_HEADER
    assert rows[1] == ["O1", "C", "5", "A03"]


def test_a_column_only_one_location_has_is_appended_not_dropped():
    header, rows, _, _ = merge_tables([
        ("NM4", [PP_HEADER, ["M1", "A", "1", "NM4"]]),
        ("A03", [PP_HEADER + ["LPN"], ["O1", "C", "1", "A03", "L9"]]),
    ])
    assert header == PP_HEADER + ["LPN"]
    assert rows == [["M1", "A", "1", "NM4", None], ["O1", "C", "1", "A03", "L9"]]


def test_empty_and_header_only_locations_count_zero():
    header, rows, counts, _ = merge_tables([
        ("S01", []),
        ("S02", [PP_HEADER]),
        ("NM4", [PP_HEADER, ["M1", "A", "1", "NM4"]]),
    ])
    assert header == PP_HEADER
    assert len(rows) == 1
    assert counts == {"S01": 0, "S02": 0, "NM4": 1}


def test_blank_rows_and_trailing_blank_cells_are_ignored():
    _, rows, counts, _ = merge_tables([
        ("NM4", [PP_HEADER + ["", ""], ["M1", "A", "1", "NM4", "", None], ["", "", None], []]),
    ])
    assert rows == [["M1", "A", "1", "NM4"]]
    assert counts == {"NM4": 1}


def test_same_order_seen_from_two_locations_is_kept_once():
    oe = ["Order No", "Status"]
    _, rows, counts, dropped = merge_tables([
        ("NM4", [oe, ["M1", "Shipped"], ["M2", "Allocated"]]),
        ("A03", [oe, ["M1", "Shipped"], ["M3", "Shipped"]]),
    ])
    assert rows == [["M1", "Shipped"], ["M2", "Allocated"], ["M3", "Shipped"]]
    assert counts == {"NM4": 2, "A03": 1}
    assert dropped == 1


def test_identical_rows_inside_one_file_are_all_kept():
    # e.g. the same SKU picked twice for one order - a real Pick Pack line each
    _, rows, _, dropped = merge_tables([
        ("NM4", [PP_HEADER, ["M1", "A", "1", "NM4"], ["M1", "A", "1", "NM4"]]),
    ])
    assert len(rows) == 2 and dropped == 0


def test_count_rows():
    assert count_rows([PP_HEADER, ["M1"], ["", ""], ["M2"]]) == 2
    assert count_rows([PP_HEADER]) == 0
    assert count_rows([]) == 0


# ── merge_location_files ─────────────────────────────────────
def _csv(path, rows, bom=False):
    with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as f:
        f.write("\r\n".join(",".join(r) for r in rows) + "\r\n")
    return str(path)


def test_csv_files_merge_into_one_csv(tmp_path):
    a = _csv(tmp_path / "a.csv", [PP_HEADER, ["M1", "A", "1", "NM4"]], bom=True)
    b = _csv(tmp_path / "b.csv", [PP_HEADER, ["O1", "C", "1", "A03"], ["O2", "D", "3", "A03"]])
    path, counts, total, dropped = merge_location_files(
        [("NM4", a), ("S01", None), ("A03", b)], str(tmp_path / "merged"))
    assert path.endswith(".csv") and path not in (a, b)
    assert counts == {"NM4": 1, "S01": 0, "A03": 2} and total == 3 and dropped == 0
    rows = read_csv_rows(path)
    assert rows[0] == PP_HEADER
    assert [r[-1] for r in rows[1:]] == ["NM4", "A03", "A03"]


def test_only_one_location_with_data_uploads_that_file_unchanged(tmp_path):
    a = _csv(tmp_path / "a.csv", [PP_HEADER, ["M1", "A", "1", "NM4"]])
    path, counts, total, _ = merge_location_files([("NM4", a), ("S01", None)],
                                                  str(tmp_path / "merged"))
    assert path == a
    assert counts == {"NM4": 1, "S01": 0} and total == 1


def test_no_location_with_data_returns_none(tmp_path):
    path, counts, total, _ = merge_location_files([("S01", None), ("S02", None)],
                                                  str(tmp_path / "merged"))
    assert path is None and total == 0 and counts == {"S01": 0, "S02": 0}


def test_mixed_file_types_are_refused(tmp_path):
    a = _csv(tmp_path / "a.csv", [PP_HEADER])
    b = tmp_path / "b.xlsx"
    b.write_bytes(b"x")
    with pytest.raises(MergeError):
        merge_location_files([("NM4", a), ("A03", str(b))], str(tmp_path / "merged"))


def test_xlsx_files_merge_keeping_the_sheet_name(tmp_path):
    from openpyxl import Workbook

    def xlsx(name, rows):
        wb = Workbook()
        ws = wb.active
        ws.title = "Export"
        for r in rows:
            ws.append(r)
        p = str(tmp_path / name)
        wb.save(p)
        return p

    oe = ["Order No", "Order Amount", "Status"]
    a = xlsx("a.xlsx", [oe, ["M1", 100.5, "Shipped"]])
    b = xlsx("b.xlsx", [oe, ["M2", 20, "Allocated"], ["M1", 100.5, "Shipped"]])
    path, counts, total, dropped = merge_location_files([("NM4", a), ("A03", b)],
                                                        str(tmp_path / "merged"))
    assert path.endswith(".xlsx")
    title, rows = read_xlsx_rows(path)
    assert title == "Export"
    assert rows == [list(oe), ["M1", 100.5, "Shipped"], ["M2", 20, "Allocated"]]
    assert counts == {"NM4": 1, "A03": 1} and total == 2 and dropped == 1
