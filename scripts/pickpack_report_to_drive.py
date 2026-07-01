"""
Nasher Miles eRetail - Pick Pack Report -> Google Drive Uploader
==================================================================
CI / GitHub Actions version. Headless Chrome, all secrets from env vars,
Drive auth via service account (see common/drive_utils.py).

Run locally for testing:
    VIN_LOGIN_ID=Sonali VIN_PASSWORD=... GDRIVE_SERVICE_ACCOUNT_JSON='{...}' \
        python scripts/pickpack_report_to_drive.py
"""

import sys
import os
import time
import json
from datetime import datetime, timedelta

from DrissionPage import ChromiumPage, ChromiumOptions

# Allow running this script directly (python scripts/pickpack_report_to_drive.py)
# by adding the repo root to the import path.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.drive_utils import get_drive_service, upload_to_drive, get_latest_downloaded_file

# ── SETTINGS ──────────────────────────────────────────────
LOGIN_ID = os.environ.get("VIN_LOGIN_ID")
PASSWORD = os.environ.get("VIN_PASSWORD")
ORG_ID   = os.environ.get("VIN_ORG_ID", "")

DATE_OPTION = "Yesterday"
LOCAL_DOWNLOAD_PATH = "/tmp/pickpack_downloads"
MAX_WAIT_SECONDS = 150

GDRIVE_FOLDER_ID = os.environ.get(
    "PICKPACK_DRIVE_FOLDER_ID", "1Xi66o4A0I3GdtlNrh61XcEWNkT5UKmey"
)

BASE = "https://nashermiles.vineretail.com/eRetailWeb"
TARGET_REPORT_NAME = "PickPackReport"


def fail(message, code=1):
    print(f"\n[✗] {message}")
    sys.exit(code)


def validate_env():
    missing = [name for name, val in [
        ("VIN_LOGIN_ID", LOGIN_ID),
        ("VIN_PASSWORD", PASSWORD),
        ("GDRIVE_SERVICE_ACCOUNT_JSON", os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")),
    ] if not val]
    if missing:
        fail(f"Missing required environment variables/secrets: {', '.join(missing)}")


def main():
    validate_env()
    os.makedirs(LOCAL_DOWNLOAD_PATH, exist_ok=True)

    print("=" * 55)
    print("  Nasher Miles - Pick Pack Report -> Google Drive")
    print(f"  Date: {DATE_OPTION}")
    print("=" * 55)

    print("\n[0] Authenticating Google Drive API (service account)...")
    drive_service = get_drive_service()
    print("    [OK] Drive API ready!")

    print("\n[1] Launching headless browser...")
    co = ChromiumOptions()
    co.headless(True)
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-gpu")
    co.set_argument("--window-size=1920,1080")
    page = ChromiumPage(addr_or_opts=co)
    page.set.download_path(LOCAL_DOWNLOAD_PATH)
    print("    [OK] Browser launched!")

    # ── STEP 2: LOGIN ────────────────────────────────────
    print("\n[2] Logging in...")
    page.get(f"{BASE}/eRetailLogin.action?popup=true")
    time.sleep(4)

    page.run_js("document.querySelector('[name=userOrgId]').removeAttribute('readonly')")
    page.run_js("document.querySelector('[name=userName]').removeAttribute('readonly')")
    page.run_js("document.querySelector('[id=password]').removeAttribute('readonly')")
    time.sleep(1)

    if ORG_ID:
        page.run_js(f"document.querySelector('[name=userOrgId]').value = '{ORG_ID}'")
    page.run_js(f"document.querySelector('[name=userName]').value = '{LOGIN_ID}'")
    page.run_js(f"document.querySelector('[id=password]').value = '{PASSWORD}'")
    time.sleep(1)
    page.run_js("document.querySelector('[name=Login][type=submit]').click()")
    time.sleep(3)

    try:
        page.handle_alert(accept=True)
        time.sleep(2)
    except Exception:
        pass
    try:
        ok = page.ele("xpath://button[contains(text(),'OK')]", timeout=3)
        if ok:
            ok.click()
            time.sleep(2)
    except Exception:
        pass

    time.sleep(4)
    if "login" in page.url.lower():
        page.quit()
        fail("Login failed — check VIN_LOGIN_ID / VIN_PASSWORD secrets.")
    print("[✓] Login successful!")

    # ── STEP 3: OPEN PICK PACK REPORT ────────────────────
    print("\n[3] Opening Pick Pack Report page...")
    page.get(f"{BASE}/reportPickPackBS")
    time.sleep(4)
    print(f"    URL: {page.url}")

    frame = page

    # ── STEP 4: SET DATE = YESTERDAY ─────────────────────
    print(f"\n[4] Setting date: {DATE_OPTION}...")

    date_input = frame.ele("xpath://label[contains(text(),'Date')]/following::input[1]", timeout=5)
    if not date_input:
        date_input = frame.ele("css:input[type=text]", timeout=5)

    if date_input:
        date_input.click()
        print("    [✓] Date field clicked")
    else:
        print("    [!] Date input field not found")
    time.sleep(2)

    clicked = frame.run_js(f"""
        var lis = document.querySelectorAll('.ranges ul li, .ranges li');
        for(var li of lis) {{
            if(li.innerText.trim() === '{DATE_OPTION}') {{
                li.click();
                return '[✓] Clicked: {DATE_OPTION}';
            }}
        }}
        var items = document.querySelectorAll('[data-range-key]');
        for(var item of items) {{
            if(item.getAttribute('data-range-key') === '{DATE_OPTION}') {{
                item.click();
                return '[✓] Clicked by key: {DATE_OPTION}';
            }}
        }}
        var avail = [];
        document.querySelectorAll('.ranges ul li,.ranges li').forEach(function(li){{avail.push(li.innerText.trim());}});
        return '[!] Not found. Available: ' + avail.join(', ');
    """)
    print(f"    {clicked}")
    time.sleep(1)

    apply_result = frame.run_js("""
        var btn = document.querySelector('.applyBtn');
        if(btn) { btn.click(); return '[✓] Apply clicked'; }
        var btns = [...document.querySelectorAll('button')];
        var ab = btns.find(b => (b.innerText||'').trim() === 'Apply');
        if(ab) { ab.click(); return '[✓] Apply clicked by text'; }
        return '[!] Apply button not found';
    """)
    print(f"    {apply_result}")
    time.sleep(2)

    date_val = frame.run_js("""
        var input = document.querySelector('input[type=text]');
        return input ? input.value : 'not found';
    """)
    print(f"    Date set to: {date_val}")

    # ── STEP 5: CLICK PRINT ───────────────────────────────
    print("\n[5] Clicking Print...")
    print_result = frame.run_js("""
        var btns = [...document.querySelectorAll('button,a')];
        var b = btns.find(x => (x.innerText||'').trim() === 'Print');
        if(b) { b.click(); return '[✓] Print clicked'; }
        return '[!] Print button not found';
    """)
    print(f"    {print_result}")
    time.sleep(3)

    # ── STEP 6: PENDING REPORTS QUEUE ─────────────────────
    print(f"\n[6] Navigating to Pending Reports queue (max wait {MAX_WAIT_SECONDS}s)...")
    page.get(f"{BASE}/pendingDisplay")
    time.sleep(4)
    print(f"    URL: {page.url}")

    start_time = time.time()
    downloaded = False
    pre_download_ts = None

    while time.time() - start_time < MAX_WAIT_SECONDS:
        status_info = page.run_js("""
            try {
                var row = document.querySelector('tr[role="row"][id="1"]');
                if(!row) return JSON.stringify({status: 'NO_ROW_1'});
                var cells = row.cells;
                var status = (cells[2].innerText || '').trim();
                var name   = (cells[5].innerText || '').trim();
                return JSON.stringify({status: status, name: name});
            } catch(e) {
                return JSON.stringify({status: 'ERROR_READING', error: e.message});
            }
        """)
        try:
            info = json.loads(status_info)
        except Exception:
            info = {}
        status = info.get('status', '')
        name = info.get('name', '')
        elapsed = int(time.time() - start_time)

        if status == "SUCCESS" and name == TARGET_REPORT_NAME:
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
            time.sleep(6)
            downloaded = True
            break
        elif status == "ERROR" and name == TARGET_REPORT_NAME:
            page.quit()
            fail("Pick Pack report generation failed on the Vinculum side (status ERROR).")
        else:
            print(f"    [{elapsed:3d}s] Row 1 is '{name}' (status: {status}) — refreshing in 10s...")
            time.sleep(10)
            page.refresh()
            time.sleep(3)

    if not downloaded:
        page.quit()
        fail(f"Report never reached SUCCESS within {MAX_WAIT_SECONDS}s.")

    # ── STEP 7: UPLOAD TO DRIVE + CLEAN UP ───────────────
    print("\n[7] Locating downloaded file and uploading to Google Drive...")
    local_file = get_latest_downloaded_file(LOCAL_DOWNLOAD_PATH, since_ts=pre_download_ts)

    if not local_file:
        page.quit()
        fail(f"Could not find the downloaded file in {LOCAL_DOWNLOAD_PATH}.")

    print(f"    [✓] Found file: {local_file}")
    drive_filename = "PickPackReport_" + (datetime.today() - timedelta(days=1)).strftime("%d.%m.%Y")
    uploaded_file_id = upload_to_drive(drive_service, local_file, drive_filename, GDRIVE_FOLDER_ID)

    page.quit()

    if not uploaded_file_id:
        fail("Drive upload failed. Check that the folder is shared with the service account's client_email.")

    print(f"    Link: https://drive.google.com/file/d/{uploaded_file_id}/view")
    try:
        os.remove(local_file)
        print(f"    [✓] Local temp file deleted: {local_file}")
    except Exception as e:
        print(f"    [!] Could not delete local temp file: {e}")

    print("\n" + "=" * 55)
    print("  [✓] SUCCESS! Pick Pack Report uploaded to Google Drive as a Sheet.")
    print(f"  Drive folder: https://drive.google.com/drive/folders/{GDRIVE_FOLDER_ID}")
    print("=" * 55)


if __name__ == "__main__":
    main()
