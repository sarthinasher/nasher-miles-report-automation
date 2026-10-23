"""
2026-10-13: Pick Pack / Sales Report catch-up + Vinculum login diagnostics.
Record: "Errors and their fixes/2026-10-13-vinculum-login-failing-and-no-catch-up.md".
"""
import json
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common import daily_export  # noqa: E402
from common.report_dates import PICK_PACK_NAME, SALES_REPORT_NAME  # noqa: E402
from common.vinculum_ui import (  # noqa: E402
    LoginError, date_range_ok, login_failure_message, set_date_range_js, vinculum_login,
)

D11, D12, D27 = date(2026, 10, 11), date(2026, 10, 12), date(2026, 9, 27)


# ── run_dates: retries, login failure stops everything ──────────
def test_all_days_exported():
    calls = []
    res = daily_export.run_dates([D11, D12], lambda drv, d, a: calls.append((d, a)), None,
                                 PICK_PACK_NAME, sleep=lambda s: None)
    assert calls == [(D11, 1), (D12, 1)]
    assert daily_export.summarize(res, PICK_PACK_NAME) == 0


def test_flaky_day_retried_with_fresh_attempt():
    calls = []

    def export(drv, d, a):
        calls.append((d, a))
        if d == D11 and a == 1:
            raise RuntimeError("Report never reached SUCCESS")

    res = daily_export.run_dates([D11, D12], export, None, PICK_PACK_NAME, sleep=lambda s: None)
    assert calls == [(D11, 1), (D11, 2), (D12, 1)]
    assert res[D11] == "OK (attempt 2)"


def test_day_failing_twice_fails_run_but_next_day_still_tried(capsys):
    def export(drv, d, a):
        if d == D11:
            raise RuntimeError("Drive upload failed")

    res = daily_export.run_dates([D11, D12], export, None, SALES_REPORT_NAME,
                                 sleep=lambda s: None)
    assert res[D11].startswith("FAILED after 2") and res[D12] == "OK (attempt 1)"
    assert daily_export.summarize(res, SALES_REPORT_NAME) == 1
    assert "11.10.2026: FAILED" in capsys.readouterr().out


def test_login_failure_is_not_retried_and_stops_the_run():
    calls = []

    def export(drv, d, a):
        calls.append((d, a))
        raise LoginError("Vinculum login failed for user 'X': alert \"Invalid password\"")

    res = daily_export.run_dates([D27, D11, D12], export, None, PICK_PACK_NAME,
                                 sleep=lambda s: None)
    assert calls == [(D27, 1)]                               # one login attempt in total
    assert res[D27].startswith("FAILED") and "Invalid password" in res[D27]
    assert res[D11] == res[D12] == "NOT RUN (Vinculum login failed)"
    assert daily_export.summarize(res, PICK_PACK_NAME) == 1


def test_main_catch_up_exports_missing_days_with_report_date_names(monkeypatch):
    monkeypatch.delenv("REPORT_DATE", raising=False)
    monkeypatch.setattr(daily_export, "datetime", type("dt", (), {
        "now": staticmethod(lambda tz=None: type("n", (), {"date": lambda s: date(2026, 10, 13)})())}))
    existing = {f"PickPackReport_{d:02d}.10.2026" for d in range(1, 11)}
    seen = []
    with pytest.raises(SystemExit) as ex:
        daily_export.main("Pick Pack Report", PICK_PACK_NAME, "folder",
                          lambda drv, d, a: seen.append(d),
                          lambda: "drive", lambda drv, folder, prefix: existing)
    assert ex.value.code == 0
    assert seen == [D11, D12]


def test_main_bad_report_date_exits_2(monkeypatch):
    monkeypatch.setenv("REPORT_DATE", "31/31/2026")
    with pytest.raises(SystemExit) as ex:
        daily_export.main("Sales", SALES_REPORT_NAME, "folder", lambda *a: None,
                          lambda: "drive", lambda *a: set())
    assert ex.value.code == 2


# ── login diagnostics ───────────────────────────────────────────
@pytest.mark.parametrize("alert, page, expect", [
    ("Your password has expired. Please change your password.", "", "EXPIRED"),
    ("", "User is locked. Contact administrator", "LOCKED / DISABLED"),
    ("Invalid Login Id or Password", "", "rejected the user / password"),
    ("", "", "no message shown"),
])
def test_login_failure_message(alert, page, expect):
    msg = login_failure_message(alert, page, "Diwakar")
    assert "user 'Diwakar'" in msg and expect in msg and "Not retried" in msg


class FakePage:
    """Just enough of a DrissionPage page for vinculum_login."""

    def __init__(self, url_after, alerts=(), page_text=""):
        self.url = "about:blank"
        self.url_after = url_after
        self.alerts = list(alerts)
        self.page_text = page_text
        self.js = []

    def get(self, url):
        self.url = url

    def run_js(self, script):
        self.js.append(script)
        if "Login][type=submit]" in script:
            self.url = self.url_after
        if "querySelectorAll(s)" in script:          # LOGIN_MESSAGE_JS
            return self.page_text
        return None

    def handle_alert(self, accept=True, timeout=None):
        return self.alerts.pop(0) if self.alerts else False

    def ele(self, *a, **k):
        return None


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    import common.vinculum_ui as vu
    monkeypatch.setattr(vu.time, "sleep", lambda s: None)


def test_login_success():
    page = FakePage("https://x/eRetailWeb/home.action")
    vinculum_login(page, "https://x/eRetailWeb", "Sonali", "pw")
    assert any("Sonali" in j for j in page.js)


def test_login_failure_reports_vinculum_reason():
    page = FakePage("https://x/eRetailWeb/eRetailLogin.action", alerts=["Invalid Password"],
                    page_text="Login failed")
    with pytest.raises(LoginError) as ex:
        vinculum_login(page, "https://x/eRetailWeb", "Diwakar", "pw")
    assert 'alert "Invalid Password"' in str(ex.value)
    assert 'page "Login failed"' in str(ex.value)


def test_password_with_quote_is_passed_safely():
    pw = "Nm'2026\\x\"y"
    page = FakePage("https://x/home")
    vinculum_login(page, "https://x", "Sonali", pw)
    js = [j for j in page.js if "[id=password]" in j and ".value" in j][0]
    assert json.dumps(pw) in js                 # escaped JS string, not '...'


# ── date range ──────────────────────────────────────────────────
def test_date_range_js_and_check():
    js = set_date_range_js("27/09/2026")
    assert 'moment("27/09/2026", "DD/MM/YYYY")' in js
    assert date_range_ok("OK|27/09/2026|27/09/2026|27/09/2026 - 27/09/2026", "27/09/2026")
    assert not date_range_ok("OK|26/09/2026|27/09/2026|", "27/09/2026")
    assert not date_range_ok("NO_DATE_INPUT", "27/09/2026")
    assert not date_range_ok(None, "27/09/2026")


# ── SharePoint sync picks the file by name (2026-10-13) ─────────
# The sync's date selection (catch-up since 2026-10-23) is tested in tests/test_sharepoint_sync.py.
def test_find_drive_file_by_name_queries_exact_name():
    from common.drive_utils import find_drive_file_by_name
    seen = {}

    class Files:
        def list(self, **kw):
            seen.update(kw)
            return type("R", (), {"execute": lambda s: {"files": [{"id": "1", "name": "12.10.2026"}]}})()

    drive = type("D", (), {"files": lambda self: Files()})()
    assert find_drive_file_by_name(drive, "F", "12.10.2026")["id"] == "1"
    assert "name='12.10.2026'" in seen["q"] and "'F' in parents" in seen["q"]
