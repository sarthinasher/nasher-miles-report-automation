"""
Nasher Miles eRetail - Pick Pack Report -> Google Drive Uploader
==================================================================
CI / GitHub Actions version. Headless Chrome, all secrets from env vars,
Drive auth via service account (see common/drive_utils.py).

Login -> Pick Pack Report -> Date = REPORT DATE -> Print -> Pending Reports ->
wait for a NEW PickPackReport SUCCESS -> download -> upload to Drive as
PickPackReport_DD.MM.YYYY (the REPORT date). Read by the GT Full Flow FMS
(Warehouse Team V / pick date X).

Which locations (2026-10-19): Vinculum exports only the session's location, so the
report is exported once per location the login can switch to (VIN_LOCATIONS: blank /
all = every one; "A03,NM4"; "all,-S01") and the files are merged into the one Drive
file (common/vinculum_ui.export_all_locations, common/report_merge.py). VIN_DRY_RUN=true
= export + merge without uploading.

Which dates (2026-10-13, common/daily_export.py): REPORT_DATE blank -> every day of
the last 7 (ending yesterday, IST) whose file is missing in Drive (normally just
yesterday); REPORT_DATE=27/09/2026 | 11/10/2026,12/10/2026 | 11/10/2026-12/10/2026
-> exactly those days. Before, only "Yesterday" was exported and a failed run lost
the day for good (27.09.2026).

Run locally for testing:
    VIN_LOGIN_ID=... VIN_PASSWORD=... GDRIVE_SERVICE_ACCOUNT_JSON='{...}' \
        REPORT_DATE=27/09/2026 python scripts/pickpack_report_to_drive.py
"""

import sys
import os
import time
import json
from datetime import datetime, timedelta, timezone

# Allow running this script directly (python scripts/pickpack_report_to_drive.py)
# by adding the repo root to the import path.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.drive_utils import (
    get_drive_service, upload_to_drive, get_latest_downloaded_file, list_file_names,
)
from common import daily_export
from common.report_dates import PICK_PACK_NAME, drive_filename, vinculum_date
from common.report_merge import dry_run, keep_dry_run_copy, merge_for_upload, remove_attempt_files
from common.vinculum_ui import (
    LOCATIONS_WITH_ORDERS, location_spec,
    SESSION_LOST_HINT, check_site_location, dismiss_top_modals, export_all_locations,
    is_no_data_message, launch_browser, read_page_message, save_debug_screenshot, session_lost,
    set_report_date, vinculum_login,
)

# ── SETTINGS ──────────────────────────────────────────────
LOGIN_ID = os.environ.get("VIN_LOGIN_ID")
PASSWORD = os.environ.get("VIN_PASSWORD")
ORG_ID   = os.environ.get("VIN_ORG_ID", "")

LOCAL_DOWNLOAD_PATH = "/tmp/pickpack_downloads"
DEBUG_DIR = os.environ.get("PICKPACK_DEBUG_DIR", "/tmp/pickpack_debug")
MAX_WAIT_SECONDS = 240
IST = timezone(timedelta(hours=5, minutes=30))   # India: no DST

GDRIVE_FOLDER_ID = os.environ.get(
    "PICKPACK_DRIVE_FOLDER_ID", "1Xi66o4A0I3GdtlNrh61XcEWNkT5UKmey"
)

BASE = "https://nashermiles.vineretail.com/eRetailWeb"
# Locations exported when VIN_LOCATIONS is not set (2026-10-21): the 6 that had Pick
# Pack lines on 19/20-Oct (the other 11 had none).
DEFAULT_LOCATIONS = LOCATIONS_WITH_ORDERS
TARGET_REPORT_NAME = "PickPackReport"

# Pending-report rows already downloaded (or already there before this run's
# request) - never taken as this date's report.
_seen_rows = set()
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


ROW1_JS = """
    try {
        var row = document.querySelector('tr[role="row"][id="1"]');
        if(!row) return JSON.stringify({status: 'NO_ROW_1', name: '', text: ''});
        var cells = row.cells;
        return JSON.stringify({status: (cells[2].innerText || '').trim(),
                               name: (cells[5].innerText || '').trim(),
                               text: (row.innerText || '').replace(/\\s+/g, ' ').trim()});
    } catch(e) {
        return JSON.stringify({status: 'ERROR_READING', name: '', text: '', error: e.message});
    }
"""


def read_row1(page):
    try:
        return json.loads(page.run_js(ROW1_JS) or "{}")
    except Exception:
        return {}


def set_yesterday_range(frame):
    """The pre-2026-10-13 path ("Yesterday" range + Apply), kept as a fallback."""
    date_input = frame.ele("xpath://label[contains(text(),'Date')]/following::input[1]", timeout=5)
    if not date_input:
        date_input = frame.ele("css:input[type=text]", timeout=5)
    if date_input:
        date_input.click()
    time.sleep(2)
    clicked = frame.run_js("""
        var lis = document.querySelectorAll('.ranges ul li, .ranges li');
        for(var li of lis) { if(li.innerText.trim() === 'Yesterday') { li.click(); return 'Clicked: Yesterday'; } }
        var items = document.querySelectorAll('[data-range-key]');
        for(var item of items) { if(item.getAttribute('data-range-key') === 'Yesterday') { item.click(); return 'Clicked by key: Yesterday'; } }
        return 'NOT_FOUND';
    """)
    time.sleep(1)
    frame.run_js("""
        var btn = document.querySelector('.applyBtn');
        if(btn) { btn.click(); return; }
        var ab = [...document.querySelectorAll('button')].find(b => (b.innerText||'').trim() === 'Apply');
        if(ab) ab.click();
    """)
    time.sleep(2)
    return clicked


def export_day(drive_service, day, attempt):
    """One attempt for one report date: fresh browser -> export -> upload."""
    print("\n[1] Launching headless browser...")
    page = launch_browser(LOCAL_DOWNLOAD_PATH)
    print("    [OK] Browser launched!")
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

    # ── STEPS 3-8 PER LOCATION, THEN MERGE (2026-10-19) ──
    # Vinculum exports only the session's location: export every location the user
    # can switch to (VIN_LOCATIONS) and merge them into the one PickPackReport_ file.
    print("\n[3] Locations...")
    drive_name = drive_filename(day, *PICK_PACK_NAME)
    parts, local_file, upload_failed = [], None, False
    try:
        parts = export_all_locations(page, BASE, lambda loc: _export_location(page, day, loc),
                                     done=_location_files.setdefault(day, {}),
                                     spec=location_spec(DEFAULT_LOCATIONS))
        local_file, _total = merge_for_upload(parts, LOCAL_DOWNLOAD_PATH, drive_name)
        if not local_file:
            fail(f"No location returned a Pick Pack file for {vinculum_date(day)}.")
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
    before = read_row1(page)
    if before.get("text"):
        _seen_rows.add(before["text"])
    print(f"    Newest pending report before this request: {before.get('text', '')[:120]!r}")

    # ── STEP 4: OPEN PICK PACK REPORT ────────────────────
    print("\n[4] Opening Pick Pack Report page...")
    page.get(f"{BASE}/reportPickPackBS")
    time.sleep(4)
    print(f"    URL: {page.url}")
    if session_lost(page):
        fail(f"Logged out of Vinculum: {SESSION_LOST_HINT}.")
    dismiss_top_modals(page, "on Pick Pack")
    if check_site_location(page, loc) is False:
        fail(f"Pick Pack's Site Location is not {loc} after switching location.")

    # ── STEP 5: SET DATE = REPORT DATE ───────────────────
    print(f"\n[5] Setting date: {day_str}...")
    ok, detail = set_report_date(page, day_str)
    print(f"    daterangepicker -> {detail}")
    if not ok:
        if day == yesterday:
            print(f"    [!] Could not set {day_str} directly — using the 'Yesterday' range.")
            print(f"    {set_yesterday_range(page)}")
        else:
            fail(f"Could not set the Pick Pack date to {day_str} ({detail}).")
    time.sleep(1)

    # ── STEP 6: CLICK PRINT ───────────────────────────────
    print("\n[6] Clicking Print...")
    print_result = page.run_js("""
        var btns = [...document.querySelectorAll('button,a')];
        var b = btns.find(x => (x.innerText||'').trim() === 'Print');
        if(b) { b.click(); return '[✓] Print clicked'; }
        return '[!] Print button not found';
    """)
    print(f"    {print_result}")
    if "not found" in str(print_result):
        fail("Print button not found on the Pick Pack page.")
    time.sleep(1)
    said = read_page_message(page, timeout=2)
    if said:
        print(f"    Vinculum said after Print: {said[:200]!r}")
    if is_no_data_message(said):
        print(f"    [✓] {loc}: no Pick Pack data for {day_str} - nothing to export here.")
        return None
    time.sleep(2)

    # ── STEP 7: PENDING REPORTS QUEUE ─────────────────────
    print(f"\n[7] Waiting in Pending Reports for a NEW {TARGET_REPORT_NAME} "
          f"(max {MAX_WAIT_SECONDS}s)...")
    page.get(f"{BASE}/pendingDisplay")
    time.sleep(4)

    start_time = time.time()
    downloaded = False
    pre_download_ts = None
    while time.time() - start_time < MAX_WAIT_SECONDS:
        if session_lost(page):
            fail(f"Logged out of Vinculum while waiting for the report: {SESSION_LOST_HINT}.")
        info = read_row1(page)
        status, name, text = info.get("status", ""), info.get("name", ""), info.get("text", "")
        elapsed = int(time.time() - start_time)
        if text and text in _seen_rows:
            print(f"    [{elapsed:3d}s] Row 1 is still an earlier report — waiting for this one...")
        elif status == "SUCCESS" and name == TARGET_REPORT_NAME:
            print(f"    [{elapsed:3d}s] Row 1 = {name}, Status: SUCCESS — downloading...")
            pre_download_ts = time.time()
            dl_result = page.run_js("""
                var row = document.querySelector('tr[role="row"][id="1"]');
                if(!row) return 'Row 1 not found at download time';
                var dlCell = row.cells[7];
                var label = dlCell ? dlCell.querySelector('label') : null;
                if(label) { label.click(); return 'Clicked download label in row 1 (cell 7)'; }
                var fallbackLabel = row.querySelector('label[onclick*="downloadReport"]');
                if(fallbackLabel) { fallbackLabel.click(); return 'Clicked download label (fallback search)'; }
                return 'Row 1 found but no download label located';
            """)
            print(f"    {dl_result}")
            _seen_rows.add(text)
            time.sleep(6)
            downloaded = True
            break
        elif status == "ERROR" and name == TARGET_REPORT_NAME:
            fail("Pick Pack report generation failed on the Vinculum side (status ERROR).")
        else:
            print(f"    [{elapsed:3d}s] Row 1 is '{name}' (status: {status}) — refreshing in 10s...")
        time.sleep(10)
        page.refresh()
        time.sleep(3)

    if not downloaded:
        fail(f"Report never reached SUCCESS within {MAX_WAIT_SECONDS}s.")

    # ── STEP 8: LOCATE THE DOWNLOADED FILE ───────────────
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
    os.makedirs(LOCAL_DOWNLOAD_PATH, exist_ok=True)
    daily_export.main("Pick Pack Report", PICK_PACK_NAME, GDRIVE_FOLDER_ID, export_day,
                      get_drive_service, list_file_names)


if __name__ == "__main__":
    main()
