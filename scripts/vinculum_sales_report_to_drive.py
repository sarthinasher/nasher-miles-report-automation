
"""
Nasher Miles eRetail - MIS Sales Report -> Google Drive Uploader
==================================================================
CI / GitHub Actions version. Headless Chrome, all secrets from env vars,
Drive auth via service account (see common/drive_utils.py).

Logs in -> Selects Sales Report -> Sets Date = Yesterday -> Exports ->
Waits for SUCCESS -> Downloads -> Uploads to Drive as a native Google Sheet
-> deletes the local temp copy.

Run locally for testing:
    VIN_LOGIN_ID=Sonali VIN_PASSWORD=... GDRIVE_SERVICE_ACCOUNT_JSON='{...}' \
        python scripts/vinculum_sales_report_to_drive.py
"""

import sys
import os
import time
import json
from datetime import datetime, timedelta

from DrissionPage import ChromiumPage, ChromiumOptions

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.drive_utils import get_drive_service, upload_to_drive, get_latest_downloaded_file

# ── SETTINGS ──────────────────────────────────────────────
LOGIN_ID = os.environ.get("VIN_LOGIN_ID")
PASSWORD = os.environ.get("VIN_PASSWORD")
ORG_ID   = os.environ.get("VIN_ORG_ID", "")

# Date options: "Yesterday" / "Last 7 Days" / "Last 30 Days" / "This Month" / "Last Month" / "Custom"
DATE_OPTION = "Yesterday"

CUSTOM_FROM = (datetime.today() - timedelta(days=2)).strftime("%d/%m/%Y")
CUSTOM_TO   = datetime.today().strftime("%d/%m/%Y")

LOCAL_DOWNLOAD_PATH = "/tmp/vinculum_sales_downloads"
MAX_WAIT_SECONDS = 120

GDRIVE_FOLDER_ID = os.environ.get(
    "VINCULUM_SALES_DRIVE_FOLDER_ID", "1iL94fl0nUOxZ_pEWH_AuxnqBYsYjsjt_"
)

BASE = "https://nashermiles.vineretail.com/eRetailWeb"


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
    print("  Nasher Miles - MIS Sales Report -> Google Drive")
    print(f"  Date: {DATE_OPTION}")
    print("=" * 55)

    print("\n[0] Authenticating Google Drive API (service account)...")
    drive_service = get_drive_service()
    print("    [OK] Drive API ready!")

    print("\n[1] Launching headless browser...")

    # GitHub Actions / Linux-safe Chromium configuration
    browser_profile = "/tmp/vinculum_chrome_profile"
    os.makedirs(browser_profile, exist_ok=True)
    
    co = ChromiumOptions()
    
    # Explicit headless mode
    co.set_argument("--headless=new")
    
    # Required/recommended for GitHub Actions Linux runners
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-gpu")
    
    # Prevent Chrome from trying to use an existing session/profile
    co.set_argument(f"--user-data-dir={browser_profile}")
    
    co.set_argument("--disable-software-rasterizer")
    co.set_argument("--disable-extensions")
    co.set_argument("--disable-background-networking")
    co.set_argument("--disable-default-apps")
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    co.set_argument("--window-size=1920,1080")
    
    # Explicitly tell DrissionPage where Chrome is
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
        print("    [!] Chrome executable not found in standard paths")
    
    try:
        page = ChromiumPage(addr_or_opts=co)
    except Exception as e:
        print("\n[ERROR] Failed to launch Chromium")
        print(f"    Chrome path: {chrome_path}")
        print(f"    Profile: {browser_profile}")
        print(f"    Error: {e}")
        raise
    
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

    # ── STEP 3: OPEN MIS REPORTS ──────────────────────────
    print("\n[3] Opening MIS Reports...")
    page.get(f"{BASE}/displayCategoryAnalysisBS?posMenu=false")
    time.sleep(5)

    # ── STEP 4: SELECT SALES REPORT ───────────────────────
    print("\n[4] Selecting Sales Report...")
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
    time.sleep(2)

    # ── STEP 5: SET DATE ───────────────────────────────────
    print(f"\n[5] Setting date: {DATE_OPTION}...")

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

    if DATE_OPTION == "Custom":
        page.run_js("""
            var lis = document.querySelectorAll('.ranges ul li, .ranges li');
            for(var li of lis) {
                if(li.innerText.trim() === 'Custom Range') { li.click(); break; }
            }
        """)
        time.sleep(1)
        page.run_js(f"""
            var s = document.querySelector('input[name="daterangepicker_start"]');
            var e = document.querySelector('input[name="daterangepicker_end"]');
            if(s) {{ s.value = '{CUSTOM_FROM}'; s.dispatchEvent(new Event('change')); }}
            if(e) {{ e.value = '{CUSTOM_TO}';   e.dispatchEvent(new Event('change')); }}
        """)
        time.sleep(1)
        print(f"    [✓] Custom: {CUSTOM_FROM} -> {CUSTOM_TO}")
    else:
        clicked = page.run_js(f"""
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

    apply_result = page.run_js("""
        var btn = document.querySelector('.applyBtn');
        if(btn) { btn.click(); return '[✓] Apply clicked'; }
        var btns = [...document.querySelectorAll('button')];
        var ab = btns.find(b => (b.innerText||'').trim() === 'Apply');
        if(ab) { ab.click(); return '[✓] Apply clicked by text'; }
        return '[!] Apply button not found';
    """)
    print(f"    {apply_result}")
    time.sleep(2)

    date_val = page.run_js("""
        var inputs = Array.from(document.querySelectorAll('input[type=text]'))
                          .filter(el => el.name !== 'daterangepicker_start' &&
                                        el.name !== 'daterangepicker_end' &&
                                        el.offsetWidth > 0);
        return inputs.length > 0 ? inputs[0].value : 'not found';
    """)
    print(f"    Date set to: {date_val}")

    # ── STEP 6: CLICK EXPORT ───────────────────────────────
    print("\n[6] Clicking Export...")
    time.sleep(1)
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

    time.sleep(3)

    # ── STEP 7: PENDING REPORTS QUEUE ──────────────────────
    print("\n[7] Navigating to Pending Reports page...")
    page.get(f"{BASE}/pendingDisplay")
    time.sleep(4)
    print(f"    URL: {page.url}")

    print(f"\n[8] Waiting for report (max {MAX_WAIT_SECONDS}s)...")
    start_time = time.time()
    downloaded = False
    pre_download_ts = None

    while time.time() - start_time < MAX_WAIT_SECONDS:
        status_info = page.run_js("""
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
        """)

        try:
            info = json.loads(status_info)
        except Exception:
            info = {}
        status = info.get('status', '')
        report_id = info.get('id', '')
        report_name = info.get('name', '')
        elapsed = int(time.time() - start_time)
        print(f"    [{elapsed:3d}s] Report #{report_id} | {report_name} | Status: {status}")

        if status == "SUCCESS":
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
            time.sleep(6)
            downloaded = True
            break

        elif status == "ERROR":
            page.quit()
            fail("Sales report generation failed on the Vinculum side (status ERROR).")

        else:
            print("       Still processing... waiting 10s")
            time.sleep(10)
            page.refresh()
            time.sleep(3)

    if not downloaded:
        page.quit()
        fail(f"Report never reached SUCCESS within {MAX_WAIT_SECONDS}s.")

    # ── STEP 9: UPLOAD TO DRIVE + CLEAN UP ────────────────
    print("\n[9] Locating downloaded file and uploading to Google Drive...")
    local_file = get_latest_downloaded_file(LOCAL_DOWNLOAD_PATH, since_ts=pre_download_ts)

    if not local_file:
        page.quit()
        fail(f"Could not find the downloaded file in {LOCAL_DOWNLOAD_PATH}.")

    print(f"    [✓] Found file: {local_file}")
    drive_filename = (datetime.today() - timedelta(days=1)).strftime("%d.%m.%Y")
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
    print("  [✓] SUCCESS! Sales Report uploaded to Google Drive as a Sheet.")
    print(f"  Drive folder: https://drive.google.com/drive/folders/{GDRIVE_FOLDER_ID}")
    print("=" * 55)


if __name__ == "__main__":
    main()
