"""remember_latest_report: the newest report of a kind that exists BEFORE this run is
marked as seen, so Inventory's first location never takes yesterday's file
(2026-10-23, "Errors and their fixes/2026-10-23-inventory-first-location-stale-report.md")."""

import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common import vinculum_ui  # noqa: E402
from common.vinculum_ui import latest_report_js, remember_latest_report  # noqa: E402

NODE = shutil.which("node")


class FakeCtx:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def run_js(self, js):
        if self.error:
            raise self.error
        return self.result


class FakePage(FakeCtx):
    def __init__(self, result=None, frames=(), error=None, get_error=None):
        super().__init__(result, error)
        self.frames, self.get_error, self.visited = list(frames), get_error, []

    def get(self, url):
        if self.get_error:
            raise self.get_error
        self.visited.append(url)

    def get_frames(self):
        return self.frames


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(vinculum_ui.time, "sleep", lambda s: None)


def test_newest_existing_report_is_marked_seen():
    seen = set()
    page = FakePage(json.dumps({"id": "1887878", "status": "SUCCESS"}))
    assert remember_latest_report(page, "https://x/eRetailWeb", "InvViewBySkuBin", seen) == "1887878"
    assert seen == {"1887878"}
    assert page.visited == ["https://x/eRetailWeb/pendingDisplay"]


def test_grid_inside_an_iframe_is_found():
    seen = set()
    page = FakePage(json.dumps({"id": ""}), frames=[FakeCtx(error=RuntimeError("x")),
                                                    FakeCtx(json.dumps({"id": "42"}))])
    assert remember_latest_report(page, "b", "InvViewBySkuBin", seen) == "42"
    assert seen == {"42"}


def test_no_earlier_report_or_any_error_never_raises():
    seen = set()
    assert remember_latest_report(FakePage(json.dumps({"id": ""})), "b", "Inv", seen) == ""
    assert remember_latest_report(FakePage("not json"), "b", "Inv", seen) == ""
    assert remember_latest_report(FakePage(get_error=RuntimeError("logged out")), "b", "Inv", seen) == ""
    assert seen == set()


_DOM_STUB = """
function cell(t){ return {innerText: t}; }
function row(cells){ return {querySelectorAll: function(){ return cells.map(cell); }}; }
var ROWS = %s;
var document = {querySelectorAll: function(){ return ROWS.map(row); }};
console.log((function(){ %s })());
"""


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_js_picks_the_highest_id_of_the_named_report():
    rows = [
        ["1,887,852", "SUCCESS", "22/10/2026 03:40 PM", "x", "InvViewBySkuBin_NM4"],
        ["1887878", "SUCCESS", "22/10/2026 03:41 PM", "x", "InvViewBySkuBin_NM9"],
        ["1894558", "SUCCESS", "23/10/2026 03:00 PM", "x", "OrderEnquiryExport"],
        ["Report Id", "Status"],                                  # header-like row
    ]
    script = _DOM_STUB % (json.dumps(rows), latest_report_js("InvViewBySkuBin"))
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(out.stdout) == {"idNum": 1887878, "id": "1887878", "status": "SUCCESS"}
