"""
Nasher Miles eRetail - MIS Sales Report -> Google Drive Uploader
==================================================================
CI / GitHub Actions version. Headless Chrome, all secrets from env vars,
Drive auth via service account (see common/drive_utils.py).

Logs in -> Selects Sales Report -> Sets Date = REPORT DATE -> Exports ->
Waits for a NEW report SUCCESS -> Downloads -> Uploads to Drive as a native Google
Sheet named DD.MM.YYYY (the REPORT = invoice date) -> deletes the local temp copy.
Read by the Apps Script Sales Master consolidation and (since 2026-10-13) directly by
the GT Full Flow FMS.

Which location (2026-10-23): the Sales Report is company-wide - every location returns
the same rows (13/10/2026: 827 from each of the 17) - so it is exported ONCE, from
SALES_LOCATION (common/vinculum_ui.single_location_choice):
  blank / unset -> A02 (DEFAULT_SALES_LOCATION)   "NM4" (any code) -> that location
  "DEFAULT"     -> no switch: the location the login lands on (NM4 today)
  "ALL"         -> the 2026-10-19 loop: once per location in VIN_LOCATIONS (blank / all =
                   every one; "A03,NM4"; "all,-S01"), merged into the one Drive file
                   (common/vinculum_ui.export_all_locations, common/report_merge.py).
The 17-location loop took 35-70 min per date and hit the 350-min timeout (record
"Errors and their fixes/2026-10-23-sales-17-location-loop-timeouts.md").
VIN_DRY_RUN=true = export (+ merge) without uploading.

Which dates (2026-10-13, common/daily_export.py): REPORT_DATE blank -> every day of
the last 7 (ending yesterday, IST) whose file is missing in Drive (normally just
yesterday); REPORT_DATE=11/10/2026 | 11/10/2026-12/10/2026 -> exactly those days.
Before, only "Yesterday" was exported, named from the runner's UTC date, and a failed
run lost the day.

Run locally for testing:
    VIN_LOGIN_ID=Sonali VIN_PASSWORD=... GDRIVE_SERVICE_ACCOUNT_JSON='{...}' \
        python scripts/vinculum_sales_report_to_drive.py
"""

import sys
import os
import time
import json
import subprocess
from datetime import datetime, timedelta, timezone

from DrissionPage import ChromiumPage, ChromiumOptions

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.drive_utils import (
    get_drive_service, upload_to_drive, get_latest_downloaded_file, list_file_names,
)
from common import daily_export
from common.report_dates import SALES_REPORT_NAME, drive_filename, vinculum_date
from common.report_merge import dry_run, keep_dry_run_copy, merge_for_upload, remove_attempt_files
from common.vinculum_ui import (
    location_spec, export_one_location, single_location_choice,
    SESSION_LOST_HINT, check_site_location, dismiss_top_modals, export_all_locations,
    is_no_data_message, read_page_message, save_debug_screenshot, session_lost, set_report_date,
    vinculum_login,
)

# ── SETTINGS ──────────────────────────────────────────────
LOGIN_ID = os.environ.get("VIN_LOGIN_ID")
PASSWORD = os.environ.get("VIN_PASSWORD")
ORG_ID   = os.environ.get("VIN_ORG_ID", "")

LOCAL_DOWNLOAD_PATH = "/tmp/vinculum_sales_downloads"
DEBUG_DIR = os.environ.get("SALES_DEBUG_DIR", "/tmp/vinculum_sales_debug")
MAX_WAIT_SECONDS = 240
IST = timezone(timedelta(hours=5, minutes=30))   # India: no DST

GDRIVE_FOLDER_ID = os.environ.get(
    "VINCULUM_SALES_DRIVE_FOLDER_ID", "1iL94fl0nUOxZ_pEWH_AuxnqBYsYjsjt_"
)

BASE = "https://nashermiles.vineretail.com/eRetailWeb"
# SALES_LOCATION blank: the one location the Sales Report is exported from (2026-10-23).
# Every location returns the full company-wide report (run 39716075711: 13/10 = 827 rows
# from each of the 17, 15/10 = 734, 16/10 = 607, ...); A02 is explicit, so the export does
# not depend on which location the login lands on.
DEFAULT_SALES_LOCATION = "A02"
# SALES_LOCATION=ALL only: locations looped when VIN_LOCATIONS is not set.
DEFAULT_LOCATIONS = "all"


# Pending-report ids already used (or already there before this run's request).
_seen_report_ids = set()
# {report date: {location: downloaded file or None}} - a retry (fresh browser) does not
# export again the locations an earlier attempt already got.
_location_files = {}


class ExportError(Exception):
    quiet = True


def fail(message):
    print(f"\n[✗] {message}")
    raise ExportError(message)


def validate_env():
    missing = [name for name, val in [
        ("VIN_LOGIN_ID", LOGIN_ID),
        ("VIN_PASSWORD", PASSWORD),
        ("GDRIVE_SERVICE_ACCOUNT_JSON", os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")),
    ] if not val]
    if missing:
        print(f"\n[✗] Missing required environment variables/secrets: {', '.join(missing)}")
        sys.exit(1)


def kill_stale_chrome():
    """Kill any leftover Chrome/chromedriver from a previous run so the debug port is free."""
    for target in ("chrome", "chromedriver"):
        subprocess.run(
            ["pkill", "-9", "-f", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    time.sleep(1)


def build_chrome_options():
    """
    CI-safe ChromiumOptions matching the known-good Pick Pack config.

    NOTE: We intentionally do NOT use co.auto_port() here. It was picking a
    "free" port by opening+closing a socket and handing that number to Chrome,
    which races on a cold CI runner: Chrome fails to bind that exact port while
    DrissionPage tries to open the DevTools websocket on it -> the
    'Handshake status 404 Not Found' error. Letting DrissionPage manage the
    port/profile itself (as the working Pick Pack script does) fixes it.
    """
    co = ChromiumOptions()

    # Headless (same flag the working script resolves to)
    co.headless(True)

    # Required / recommended on CI Linux runners
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-gpu")
    co.set_argument("--disable-software-rasterizer")

    # Stability / determinism
    co.set_argument("--disable-extensions")
    co.set_argument("--disable-background-networking")
    co.set_argument("--disable-default-apps")
    co.set_argument("--disable-popup-blocking")
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    co.set_argument("--window-size=1920,1080")

    # Explicitly locate Chrome
    chrome_paths = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    chrome_path = next((p for p in chrome_paths if os.path.exists(p)), None)
    if chrome_path:
        print(f"    Chrome found: {chrome_path}")
        co.set_browser_path(chrome_path)
    else:
        print("    [!] Chrome executable not found in standard paths — letting DrissionPage auto-detect")

    return co


def launch_browser(max_attempts=3):
    """Launch Chromium with retries. A cold CI runner occasionally loses the first handshake."""
    last_err = None
    for attempt in range(1, max_attempts + 1):
        print(f"    Launch attempt {attempt}/{max_attempts}...")
        kill_stale_chrome()
        co = build_chrome_options()
        try:
            page = ChromiumPage(addr_or_opts=co)
            print("    [OK] Browser launched!")
            return page
        except Exception as e:
            last_err = e
            print(f"    [!] Attempt {attempt} failed: {e}")
            time.sleep(3)
    print("\n[ERROR] Failed to launch Chromium after all retries")
    raise last_err


TOP_REPORT_JS = """
    try {
        var rows = document.querySelectorAll('tr[role="row"]');
        for(var row of rows) {
            var cells = row.querySelectorAll('td');
            if(cells.length > 2) {
                var reportId   = cells[0] ? cells[0].innerText.trim() : '';
                var status     = cells[1] ? cells[1].innerText.trim() : '';
                var reportName = cells[3] ? cells[3].innerText.trim() : '';
                if(reportId && status) {
                    return JSON.stringify({id: reportId, status: status, name: reportName});
                }
            }
        }
        return JSON.stringify({id:'', status:'NOT_FOUND', name:''});
    } catch(e) {
        return JSON.stringify({id:'', status:'ERROR_READING', name:e.message});
    }
"""


def read_top_report(page):
    try:
        return json.loads(page.run_js(TOP_REPORT_JS) or "{}")
    except Exception:
        return {}


def set_yesterday_range(page):
    """The pre-2026-10-13 path ("Yesterday" range + Apply), kept as a fallback."""
    page.run_js("""
        var inputs = Array.from(document.querySelectorAll('input[type=text]'))
                          .filter(function(el) {
                              return el.name !== 'daterangepicker_start' &&
                                     el.name !== 'daterangepicker_end' &&
                                     el.offsetWidth > 0;
                          });
        if(inputs.length > 0) {
            inputs[0].focus();
            inputs[0].click();
            try { $(inputs[0]).trigger('click'); } catch(e){}
        }
    """)
    time.sleep(2)
    clicked = page.run_js("""
        var lis = document.querySelectorAll('.ranges ul li, .ranges li');
        for(var li of lis) { if(li.innerText.trim() === 'Yesterday') { li.click(); return 'Clicked: Yesterday'; } }
        var items = document.querySelectorAll('[data-range-key]');
        for(var item of items) { if(item.getAttribute('data-range-key') === 'Yesterday') { item.click(); return 'Clicked by key: Yesterday'; } }
        return 'NOT_FOUND';
    """)
    time.sleep(1)
    page.run_js("""
        var btn = document.querySelector('.applyBtn');
        if(btn) { btn.click(); return; }
        var ab = [...document.querySelectorAll('button')].find(b => (b.innerText||'').trim() === 'Apply');
        if(ab) ab.click();
    """)
    time.sleep(2)
    return clicked


def export_day(drive_service, day, attempt):
    """One attempt for one report (invoice) date: fresh browser -> export -> upload."""
    print("\n[1] Launching headless browser...")
    page = launch_browser()
    page.set.download_path(LOCAL_DOWNLOAD_PATH)
    try:
        _export_steps(page, drive_service, day)
    except Exception:
        save_debug_screenshot(page, DEBUG_DIR, f"fail_{day:%Y-%m-%d}_attempt{attempt}.png")
        raise
    finally:
        try:
            page.quit()
        except Exception:
            pass


def _export_steps(page, drive_service, day):
    # ── STEP 2: LOGIN (LoginError stops the whole run) ───
    print("\n[2] Logging in...")
    vinculum_login(page, BASE, LOGIN_ID, PASSWORD, ORG_ID)
    dismiss_top_modals(page, "after login")

    # ── STEPS 3-8 FOR ONE LOCATION (2026-10-23) ─────────
    # The Sales Report is company-wide: export it once, from SALES_LOCATION (A02).
    # SALES_LOCATION=ALL keeps the 2026-10-19 loop over every location + merge.
    print("\n[3] Location...")
    drive_name = drive_filename(day, *SALES_REPORT_NAME)
    mode, sales_loc = single_location_choice(os.environ.get("SALES_LOCATION"),
                                             DEFAULT_SALES_LOCATION)
    print(f"    SALES_LOCATION={os.environ.get('SALES_LOCATION') or '(blank)'} -> "
          + {"single": f"export from {sales_loc} only",
             "session": "export from the session's own location only (no switch)",
             "all": "export every location and merge (VIN_LOCATIONS)"}[mode])
    done = _location_files.setdefault(day, {})
    parts, local_file, upload_failed = [], None, False
    try:
        if mode == "all":
            parts = export_all_locations(page, BASE, lambda loc: _export_location(page, day, loc),
                                         done=done, spec=location_spec(DEFAULT_LOCATIONS))
        else:
            parts = export_one_location(page, BASE, lambda loc: _export_location(page, day, loc),
                                        loc=sales_loc, done=done)
        local_file, _total = merge_for_upload(parts, LOCAL_DOWNLOAD_PATH, drive_name)
        if not local_file:
            fail(f"No location returned a Sales Report file for {vinculum_date(day)}.")
        if dry_run():
            keep_dry_run_copy(local_file, DEBUG_DIR, drive_name)
            return

        # ── STEP 9: UPLOAD TO DRIVE ──────────────────────
        print("\n[9] Uploading to Google Drive...")
        uploaded_file_id = upload_to_drive(drive_service, local_file, drive_name, GDRIVE_FOLDER_ID)
        if not uploaded_file_id:
            upload_failed = True        # the retry reuses the per-location files
            fail("Drive upload failed. Check that the folder is shared with the service account's client_email.")
        print(f"    Link: https://drive.google.com/file/d/{uploaded_file_id}/view")
        print(f"  [✓] {drive_name} uploaded to Google Drive as a Sheet.")
    finally:
        remove_attempt_files(parts, local_file, keep_parts=upload_failed)


def _export_location(page, day, loc):
    """Steps 3-8 for the session's current location: returns the downloaded file,
    or None when Vinculum says there is no data for this location."""
    day_str = vinculum_date(day)
    yesterday = datetime.now(IST).date() - timedelta(days=1)

    # ── STEP 3: NOTE THE NEWEST PENDING REPORT BEFORE OUR REQUEST ─
    page.get(f"{BASE}/pendingDisplay")
    time.sleep(4)
    before = read_top_report(page)
    if before.get("id"):
        _seen_report_ids.add(before["id"])
    print(f"    Newest pending report before this request: #{before.get('id', '')}")

    # ── STEP 4: OPEN MIS REPORTS + SELECT SALES REPORT ────
    print("\n[4] Opening MIS Reports and selecting Sales Report...")
    page.get(f"{BASE}/displayCategoryAnalysisBS?posMenu=false")
    time.sleep(5)
    if session_lost(page):
        fail(f"Logged out of Vinculum: {SESSION_LOST_HINT}.")
    dismiss_top_modals(page, "on MIS Reports")
    if check_site_location(page, loc) is False:
        fail(f"The MIS report's Site Location is not {loc} after switching location.")
    result = page.run_js("""
        var targetSelect = null, targetValue = null;
        document.querySelectorAll('select').forEach(function(s) {
            Array.from(s.options).forEach(function(o) {
                if(o.text.trim().toLowerCase() === 'sales report') {
                    targetSelect = s; targetValue = o.value;
                }
            });
        });
        if(targetSelect) {
            $(targetSelect).val(targetValue).trigger('change.select2').trigger('change');
            return '[✓] Sales Report selected (value=' + targetValue + ')';
        }
        return '[!] Not found';
    """)
    print(f"    {result}")
    if "Not found" in str(result):
        fail("'Sales Report' not found in the MIS report list.")
    time.sleep(2)

    # ── STEP 5: SET DATE = REPORT DATE ────────────────────
    print(f"\n[5] Setting date: {day_str}...")
    ok, detail = set_report_date(page, day_str)
    print(f"    daterangepicker -> {detail}")
    if not ok:
        if day == yesterday:
            print(f"    [!] Could not set {day_str} directly — using the 'Yesterday' range.")
            print(f"    {set_yesterday_range(page)}")
        else:
            fail(f"Could not set the Sales Report date to {day_str} ({detail}).")
    time.sleep(1)

    # ── STEP 6: CLICK EXPORT ───────────────────────────────
    print("\n[6] Clicking Export...")
    try:
        btn = page.ele("xpath://button[contains(.,'Export')] | //a[contains(.,'Export')]", timeout=5)
        if btn:
            btn.click()
            print("    [✓] Export clicked!")
        else:
            raise Exception("not found")
    except Exception:
        page.run_js("""
            var b = [...document.querySelectorAll('button,a')]
                        .find(x => (x.innerText||'').trim().toLowerCase() === 'export');
            if(b) b.click();
        """)
        print("    [✓] Export JS clicked")
    time.sleep(1)
    said = read_page_message(page, timeout=2)
    if said:
        print(f"    Vinculum said after Export: {said[:200]!r}")
    if is_no_data_message(said):
        print(f"    [✓] {loc}: no sales for {day_str} - nothing to export here.")
        return None
    time.sleep(2)

    # ── STEP 7: PENDING REPORTS QUEUE ──────────────────────
    print(f"\n[7] Waiting in Pending Reports for a NEW report (max {MAX_WAIT_SECONDS}s)...")
    page.get(f"{BASE}/pendingDisplay")
    time.sleep(4)

    start_time = time.time()
    downloaded = False
    pre_download_ts = None
    while time.time() - start_time < MAX_WAIT_SECONDS:
        if session_lost(page):
            fail(f"Logged out of Vinculum while waiting for the report: {SESSION_LOST_HINT}.")
        info = read_top_report(page)
        status, report_id, report_name = (info.get("status", ""), info.get("id", ""),
                                          info.get("name", ""))
        elapsed = int(time.time() - start_time)
        print(f"    [{elapsed:3d}s] Report #{report_id} | {report_name} | Status: {status}")
        if report_id and report_id in _seen_report_ids:
            print("       (an earlier report — waiting for this request's one)")
        elif status == "SUCCESS":
            print("\n    [✓] Report is ready! Downloading...")
            pre_download_ts = time.time()
            time.sleep(1)
            dl_result = page.run_js("""
                var rows = document.querySelectorAll('tr[role="row"]');
                for(var row of rows) {
                    var cells = row.querySelectorAll('td');
                    if(cells.length > 1) {
                        var status = cells[1] ? cells[1].innerText.trim() : '';
                        if(status === 'SUCCESS') {
                            var dlLabel = row.querySelector('label[onclick*="downloadReport"]');
                            var dlImg   = row.querySelector('img[src*="Download"], img[src*="download"]');
                            var dlLink  = row.querySelector('a[href*="reports/"]');
                            if(dlLabel) { dlLabel.click(); return 'Clicked download label'; }
                            if(dlImg)   { dlImg.click();   return 'Clicked download img'; }
                            if(dlLink)  { window.location.href = dlLink.href; return 'Navigated to: ' + dlLink.href; }
                            var dlCell = cells[5];
                            if(dlCell) {
                                var any = dlCell.querySelector('label,a,img,button');
                                if(any) { any.click(); return 'Clicked element in download cell'; }
                            }
                            return 'SUCCESS row found but no download element';
                        }
                    }
                }
                return 'No SUCCESS row found';
            """)
            print(f"    {dl_result}")
            if report_id:
                _seen_report_ids.add(report_id)
            time.sleep(6)
            downloaded = True
            break
        elif status == "ERROR":
            fail("Sales report generation failed on the Vinculum side (status ERROR).")
        else:
            print("       Still processing... waiting 10s")
        time.sleep(10)
        page.refresh()
        time.sleep(3)

    if not downloaded:
        fail(f"Report never reached SUCCESS within {MAX_WAIT_SECONDS}s.")

    # ── STEP 8: LOCATE THE DOWNLOADED FILE ────────────────
    print("\n[8] Locating downloaded file...")
    local_file = None
    deadline = time.time() + 60
    while time.time() < deadline and not local_file:
        local_file = get_latest_downloaded_file(LOCAL_DOWNLOAD_PATH, since_ts=pre_download_ts)
        if not local_file:
            time.sleep(2)
    if not local_file:
        fail(f"Could not find the downloaded file in {LOCAL_DOWNLOAD_PATH}.")

    print(f"    [✓] Found file: {local_file}")
    return local_file


def main():
    validate_env()
    try:   # a bad SALES_LOCATION fails here, before any Vinculum login
        single_location_choice(os.environ.get("SALES_LOCATION"), DEFAULT_SALES_LOCATION)
    except Exception as e:
        print(f"\n[✗] SALES_LOCATION: {e}")
        sys.exit(2)
    os.makedirs(LOCAL_DOWNLOAD_PATH, exist_ok=True)
    daily_export.main("MIS Sales Report", SALES_REPORT_NAME, GDRIVE_FOLDER_ID, export_day,
                      get_drive_service, list_file_names)


if __name__ == "__main__":
    main()
