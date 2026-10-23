"""
Nasher Miles eRetail - Inventory View (By SKU BIN) -> Google Drive Uploader
==============================================================================
CI / GitHub Actions version. Headless Chrome, all secrets from env vars,
Drive auth via service account (see common/drive_utils.py).

Logs in -> WMS -> Inventory View -> By SKU BIN -> Search -> Export ->
Waits for SUCCESS -> Downloads -> Uploads to Drive as a native Google Sheet
-> deletes the local temp copy.

Which locations (2026-10-19): Vinculum exports only the session's location, so the
inventory is exported once per location the login can switch to (VIN_LOCATIONS: blank /
all = every one; "A03,NM4"; "all,-S01") and the files are merged into the one Drive
file (common/vinculum_ui.export_all_locations, common/report_merge.py). VIN_DRY_RUN=true
= export + merge without uploading.


Run locally for testing:
    VIN_LOGIN_ID=Sonali VIN_PASSWORD=... GDRIVE_SERVICE_ACCOUNT_JSON='{...}' \
        python scripts/inventory_report_to_drive.py
"""

import sys
import os
import time
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from DrissionPage import ChromiumPage, ChromiumOptions

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.drive_utils import get_drive_service, upload_to_drive, get_latest_downloaded_file
from common.report_merge import dry_run, keep_dry_run_copy, merge_for_upload, remove_files
from common.vinculum_ui import (
    location_spec,
    SESSION_LOST_HINT, LocationError, LoginError, dismiss_top_modals, export_all_locations,
    remember_latest_report, session_lost, vinculum_login,
)

# ── SETTINGS ──────────────────────────────────────────────
LOGIN_ID = os.environ.get("VIN_LOGIN_ID")
PASSWORD = os.environ.get("VIN_PASSWORD")
ORG_ID   = os.environ.get("VIN_ORG_ID", "")

MAX_WAIT_SECONDS = 120
MAX_SEARCH_TRIES = 3

GDRIVE_FOLDER_ID = os.environ.get(
    "INVENTORY_DRIVE_FOLDER_ID", "1BSVYTIkVTQ72q5p1wkl9ld_eOVEjAfIc"
)

LOCAL_DOWNLOAD_PATH = "/tmp/inventory_downloads"
BASE = "https://nashermiles.vineretail.com/eRetailWeb"
# Locations exported when VIN_LOCATIONS is not set: all of them (stock can sit at a
# location that has no orders).
DEFAULT_LOCATIONS = "all"
IST = ZoneInfo("Asia/Kolkata")
_seen_report_ids = set()     # Pending Report ids not to take: downloaded, or there before this run


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
    print("  Nasher Miles - Inventory View (By SKU BIN) -> Drive")
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
    try:
        vinculum_login(page, BASE, LOGIN_ID, PASSWORD, ORG_ID)
    except LoginError as e:
        page.quit()
        fail(str(e))
    home_url = page.url
    # 2026-10-23: the newest InvViewBySkuBin from before this run (yesterday's stock) is
    # never taken as the first location's report.
    remember_latest_report(page, BASE, "InvViewBySkuBin", _seen_report_ids)

    # ── STEPS 3-9 PER LOCATION, THEN MERGE (2026-10-19) ──
    # Vinculum shows only the session's location: export every location the user can
    # switch to (VIN_LOCATIONS) and merge them into the one Drive file.
    print("\n[2b] Locations...")
    drive_filename = datetime.now(IST).strftime("%d-%m-%Y_%H-%M")
    parts, local_file = [], None
    try:
        parts = export_all_locations(page, BASE, lambda loc: _export_location(page, loc, home_url),
                                     spec=location_spec(DEFAULT_LOCATIONS))
        local_file, _total = merge_for_upload(parts, LOCAL_DOWNLOAD_PATH, drive_filename)
    except LocationError as e:
        page.quit()
        fail(str(e))
    page.quit()
    if not local_file:
        fail("No location returned an inventory file.")
    if dry_run():
        keep_dry_run_copy(local_file, os.environ.get("INVENTORY_DEBUG_DIR", "/tmp/inventory_debug"),
                          drive_filename)
        remove_files([p for _, p in parts] + [local_file])
        return

    # ── STEP 10: UPLOAD TO DRIVE + CLEAN UP ────────────────
    print("\n[10] Uploading to Google Drive...")
    uploaded_file_id = upload_to_drive(drive_service, local_file, drive_filename, GDRIVE_FOLDER_ID)
    remove_files([p for _, p in parts] + [local_file])
    if not uploaded_file_id:
        fail("Drive upload failed. Check that the folder is shared with the service account's client_email.")
    print(f"    Link: https://drive.google.com/file/d/{uploaded_file_id}/view")

    print("\n" + "=" * 55)
    print("  [✓] SUCCESS! Inventory Report uploaded to Google Drive as a Sheet.")
    print(f"  Drive folder: https://drive.google.com/drive/folders/{GDRIVE_FOLDER_ID}")
    print("=" * 55)


ORG_LOC_JS = "var e = document.getElementById('orgLocKey'); return e ? e.value : '';"


def _export_location(page, loc, home_url):
    """Steps 3-9 for the session's current location (loc): returns the downloaded
    file, or None when Vinculum has no inventory there."""
    # Each location starts from the landing page (the WMS menu lives there); its
    # hidden orgLocKey ("NM4_NASA") proves the location switch took.
    if (page.url or "") != home_url:
        page.get(home_url)
        time.sleep(5)
        dismiss_top_modals(page, "on the landing page")
    try:
        org_loc = (page.run_js(ORG_LOC_JS) or "").strip().upper()
    except Exception:
        org_loc = ""
    print(f"    Landing page location key: {org_loc or '?'}")
    if session_lost(page):
        page.quit()
        fail(f"Logged out of Vinculum: {SESSION_LOST_HINT}.")
    if org_loc and not org_loc.startswith(loc.upper() + "_"):
        page.quit()
        fail(f"Landing page is on {org_loc}, not {loc} - the location switch did not take.")

    # ── STEP 3: NAVIGATE TO WMS > INVENTORY > INVENTORY VIEW ─
    print("\n[3] Navigating to WMS > Inventory > Inventory View...")

    nav_result = page.run_js("""
        function findByText(tag, text) {
            var els = document.querySelectorAll(tag);
            for (var el of els) {
                if ((el.innerText || el.textContent || '').trim() === text) return el;
            }
            return null;
        }
        var direct = findByText('a', 'Inventory View') || findByText('span', 'Inventory View') || findByText('li', 'Inventory View');
        if (direct) { direct.click(); return '[✓] Clicked existing "Inventory View" link/tab'; }
        return '[!] "Inventory View" not immediately visible — menu may need to be opened first';
    """)
    print(f"    {nav_result}")

    if "not immediately visible" in nav_result:
        print("    Trying to open sidebar menu to reveal 'Inventory View'...")
        page.run_js("""
            document.querySelectorAll('.sidebar i, .sidebar a, [class*=sidebar] i, [class*=menu] i').forEach(function(el){
                try { el.click(); } catch(e) {}
            });
        """)
        time.sleep(2)
        nav_result2 = page.run_js("""
            function findByText(tag, text) {
                var els = document.querySelectorAll(tag);
                for (var el of els) {
                    if ((el.innerText || el.textContent || '').trim() === text) return el;
                }
                return null;
            }
            var el = findByText('a', 'Inventory View') || findByText('span', 'Inventory View') || findByText('li', 'Inventory View');
            if (el) { el.click(); return '[✓] Clicked "Inventory View" after opening sidebar menu'; }
            return '[!] Still not found — manual selector fix needed';
        """)
        print(f"    {nav_result2}")

    time.sleep(4)

    # ── STEP 3b: FIND THE FRAME THAT ACTUALLY HOSTS INVENTORY VIEW ──
    print("\n[3b] Locating Inventory View frame context...")

    def find_context(marker_text, max_wait=15):
        check_js = f"""
            var els = document.querySelectorAll('a,span,li,button,div');
            for (var el of els) {{
                if ((el.innerText || el.textContent || '').trim() === {json.dumps(marker_text)}) {{
                    var r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return true;
                }}
            }}
            return false;
        """
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                if page.run_js(check_js):
                    return page, 'main page'
            except Exception:
                pass
            try:
                for i, fr in enumerate(page.get_frames()):
                    try:
                        if fr.run_js(check_js):
                            return fr, f'iframe #{i}'
                    except Exception:
                        continue
            except Exception:
                pass
            time.sleep(1)
        return None, None

    ctx, ctx_desc = find_context('By SKU BIN')
    if ctx is None:
        page.quit()
        fail("Could not find 'By SKU BIN' in the main page or any iframe.")
    print(f"    [✓] Inventory View content found in: {ctx_desc}")

    # ── STEP 4: CLICK "By SKU BIN" SUB-TAB ───────────────
    print("\n[4] Selecting 'By SKU BIN' tab...")

    def js_click_visible_text(texts, tags=('a', 'span', 'li', 'button', 'div'), max_wait=15):
        texts_json = json.dumps(texts)
        tags_json = json.dumps(tags)
        deadline = time.time() + max_wait
        while time.time() < deadline:
            result = ctx.run_js(f"""
                var texts = {texts_json};
                var tags = {tags_json};
                for (var t of tags) {{
                    var els = document.querySelectorAll(t);
                    for (var el of els) {{
                        var txt = (el.innerText || el.textContent || '').trim();
                        if (texts.indexOf(txt) !== -1) {{
                            var r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {{
                                el.click();
                                return 'clicked:' + txt;
                            }}
                        }}
                    }}
                }}
                return 'not_ready';
            """)
            if result and result.startswith('clicked:'):
                return True, result
            time.sleep(1)
        return False, 'timeout'

    ok, info = js_click_visible_text(['By SKU BIN'])
    if ok:
        print(f"    [✓] {info}")
    else:
        page.quit()
        fail(f"Could not click 'By SKU BIN' tab ({info}).")

    time.sleep(2)

    # ── HELPERS: real data-row counting + "No data found" detection ──
    def count_data_rows():
        return ctx.run_js("""
            function countIn(doc){
                var best = 0;
                var selectors = ['tr[role="row"]', 'tbody tr', 'tr'];
                for (var s of selectors){
                    var rows = doc.querySelectorAll(s);
                    var n = 0;
                    for (var row of rows){
                        if (row.querySelector('th')) continue;
                        var cells = row.querySelectorAll('td');
                        if (cells.length < 2) continue;
                        var txt = (row.innerText || '').replace(/\\s+/g,'');
                        if (txt.length > 3) n++;
                    }
                    if (n > best) best = n;
                }
                return best;
            }
            return countIn(document);
        """) or 0

    def has_no_data_popup():
        js = """
            var body = (document.body ? document.body.innerText : '') || '';
            return body.indexOf('No data found') !== -1;
        """
        try:
            if ctx.run_js(js):
                return True
        except Exception:
            pass
        try:
            return bool(page.run_js(js))
        except Exception:
            return False

    # ── STEP 5+6: SEARCH, THEN WAIT FOR REAL DATA (with retry) ───
    print("\n[5/6] Searching and waiting for real inventory data...")

    data_ready = False
    empty_every_try = True               # 'No data found' on every try = nothing at this location
    for attempt in range(1, MAX_SEARCH_TRIES + 1):
        print(f"\n    --- Search attempt {attempt}/{MAX_SEARCH_TRIES} ---")

        ok, info = js_click_visible_text(['Search'], tags=('button', 'a'))
        if ok:
            print(f"    [✓] {info}")
        else:
            print(f"    [!] Could not click Search ({info}) — retrying...")
            empty_every_try = False
            time.sleep(2)
            continue

        time.sleep(6)

        found_rows = 0
        empty_flagged = False
        for i in range(20):
            if has_no_data_popup():
                empty_flagged = True
                break
            found_rows = count_data_rows()
            if found_rows > 0:
                break
            time.sleep(1)

        if found_rows > 0:
            time.sleep(2)
            found_rows = count_data_rows()
            print(f"    [✓] Real data present: {found_rows} row(s)")
            data_ready = True
            break
        elif empty_flagged:
            print("    [!] 'No data found in the grid' — search returned nothing, retrying...")
            time.sleep(3)
        else:
            print(f"    [!] Grid still empty after wait (data rows detected: {found_rows}) — retrying search...")
            empty_every_try = False
            time.sleep(3)

    if not data_ready and empty_every_try:
        print(f"    [✓] {loc}: Vinculum says 'No data found' on every try - no inventory here.")
        return None
    if not data_ready:
        page.quit()
        fail(f"{loc}: grid never returned data after all retries. Not exporting an empty report.")

    print("Waiting for AJAX to finish...")
    while True:
        active = ctx.run_js("""
            return window.jQuery ? jQuery.active : 0;
        """)
        if active == 0:
            break
        time.sleep(1)
    print("AJAX finished.")
    time.sleep(1)

    # ── STEP 7: CLICK EXPORT ───────────────────────────────
    print("\n[7] Clicking Export...")

    for _ in range(20):
        disabled = ctx.run_js("""
            var btn = document.getElementById("exportInventorySKUBINButton");
            if (!btn) return "NO_BUTTON";
            return btn.disabled;
        """)
        if disabled is False:
            break
        time.sleep(1)

    export_clicked = False
    try:
        btn = ctx.ele("#exportInventorySKUBINButton", timeout=8)
        if btn:
            btn.hover()
            time.sleep(0.5)
            btn.click(by_js=False)
            export_clicked = True
            print("    [✓] Export clicked.")
            time.sleep(2)
        else:
            print("    [!] Export button not found.")
    except Exception as e:
        print(f"    [!] Export click failed: {e}")

    if not export_clicked:
        page.quit()
        fail("Could not click Export.")

    # ── STEP 8: OPEN PENDING REPORT PAGE ───────────────────
    print("\n[8] Opening Pending Report page...")
    page.get(f"{BASE}/pendingDisplay")
    time.sleep(5)

    def find_pending_ctx(max_wait=15):
        markers = ["report request date", "report names", "pending report"]
        check_js = f"""
            var markers = {json.dumps(markers)};
            var els = document.querySelectorAll('th,td,span,div,h1,h2,a');
            for (var el of els) {{
                var t = (el.innerText || el.textContent || '').trim().toLowerCase();
                if (!t) continue;
                for (var m of markers) {{ if (t.indexOf(m) !== -1) return true; }}
            }}
            return false;
        """
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                if page.run_js(check_js):
                    return page, 'main page'
            except Exception:
                pass
            try:
                for i, fr in enumerate(page.get_frames()):
                    try:
                        if fr.run_js(check_js):
                            return fr, f'iframe #{i}'
                    except Exception:
                        continue
            except Exception:
                pass
            time.sleep(1)
        return None, None

    pctx, pctx_desc = find_pending_ctx()
    if pctx is None:
        print("    [!] Could not locate the pending-report grid frame — using main page.")
        pctx = page
        pctx_desc = 'main page (fallback)'
    print(f"    [✓] Pending grid found in: {pctx_desc}")

    # ── STEP 9: WAIT FOR SUCCESS, THEN DOWNLOAD ────────────
    print(f"\n[9] Waiting for report (max {MAX_WAIT_SECONDS}s)...")

    def get_latest_report():
        raw = pctx.run_js("""
            try {
                var rows = document.querySelectorAll('tr[role="row"]');
                var latest = null;
                for (var row of rows) {
                    var cells = row.querySelectorAll("td");
                    if (cells.length < 11) continue;

                    var reportName = cells[4].innerText.trim();
                    if (reportName.indexOf("InvViewBySkuBin") === -1) continue;

                    var reportId = cells[0].innerText.trim();
                    var status   = cells[1].innerText.trim();
                    var fileId   = cells[10].innerText.trim();
                    var idNum    = parseInt(reportId.replace(/,/g, ""), 10);

                    if (latest == null || idNum > latest.idNum) {
                        latest = {idNum: idNum, id: reportId, fileId: fileId, status: status};
                    }
                }
                if (latest) return JSON.stringify(latest);
                return JSON.stringify({id: "", fileId: "", status: "NOT_FOUND"});
            } catch (e) {
                return JSON.stringify({id: "", fileId: "", status: "ERROR", error: e.toString()});
            }
        """)
        try:
            return json.loads(raw)
        except Exception:
            return {"id": "", "fileId": "", "status": "ERROR"}

    def wait_for_new_file(folder, since_ts, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            f = get_latest_downloaded_file(folder, since_ts=since_ts)
            if f and os.path.getsize(f) > 0:
                time.sleep(1)
                return f
            time.sleep(1)
        return None

    start_time = time.time()
    downloaded = False
    downloaded_file = None
    pre_download_ts = time.time()

    time.sleep(5)

    while time.time() - start_time < MAX_WAIT_SECONDS:
        pctx, _ = find_pending_ctx(max_wait=5)
        if pctx is None:
            pctx = page

        info = get_latest_report()
        report_id = info.get("id", "")
        file_id = info.get("fileId", "")
        status = info.get("status", "")
        elapsed = int(time.time() - start_time)
        if report_id and report_id in _seen_report_ids:
            status = f"(#{report_id} is an earlier location's report - waiting for this one)"
        print(f"[{elapsed:3d}s] Latest Report #{report_id}  Status: {status}")

        if status == "SUCCESS":
            _seen_report_ids.add(report_id)
            print("\n[✓] SUCCESS — triggering downloadReport()...")
            pre_download_ts = time.time()
            clicked = pctx.run_js(f"""
                try {{
                    if (typeof downloadReport !== "function") return "NO_FUNCTION";
                    downloadReport(
                        {json.dumps(report_id)},
                        {json.dumps(file_id)},
                        {{stopPropagation: function(){{}}, preventDefault: function(){{}}}}
                    );
                    return "DOWNLOAD_CALLED";
                }} catch (e) {{
                    return e.toString();
                }}
            """)
            print(f"    downloadReport -> {clicked}")

            if clicked == "DOWNLOAD_CALLED":
                downloaded_file = wait_for_new_file(LOCAL_DOWNLOAD_PATH, pre_download_ts, timeout=60)
                if downloaded_file:
                    downloaded = True
                    print(f"    [✓] File downloaded: {downloaded_file}")
                else:
                    print("    [!] downloadReport fired but no file appeared in time.")
            break

        print("    Waiting 10 seconds...")
        time.sleep(10)
        page.refresh()
        time.sleep(3)

    if not downloaded:
        page.quit()
        fail(f"Report never downloaded within {MAX_WAIT_SECONDS}s.")

    local_file = downloaded_file or get_latest_downloaded_file(LOCAL_DOWNLOAD_PATH, since_ts=pre_download_ts)
    if not local_file:
        page.quit()
        fail(f"Could not find the downloaded file in {LOCAL_DOWNLOAD_PATH}.")
    print(f"    [✓] Found file: {local_file}")
    return local_file


if __name__ == "__main__":
    main()
