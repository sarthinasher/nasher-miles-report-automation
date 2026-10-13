"""
Nasher Miles eRetail - SKU Master Export -> Google Drive Uploader
=====================================================================
CI / GitHub Actions version. Headless Chrome, all secrets from env vars,
Drive auth via service account (see common/drive_utils.py).

Login -> Master -> SKU Master -> Search -> (wait for data) -> Export ->
(dialog) Export Type = Normal Export + Select ALL fields -> Export ->
Pending Report -> wait for a NEW SkuEnquiryExport SUCCESS ->
downloadReport() -> upload to Google Drive as "SKU Master DD-MM-YYYY" (run date, IST).

The SKU Master is a snapshot of the current catalogue (not a date range), so a
missed week cannot be exported "as of" a past day — instead:
  scheduled runs (several per week, see the workflow) export only if no
      "SKU Master DD-MM-YYYY" file dated this week (Monday..today, IST) is in
      Drive yet — so a failed/cancelled/skipped run is repaired by the next one;
  manual runs (workflow_dispatch) always export, unless SKIP_EXISTING=true.
Each run gets up to MAX_ATTEMPTS fresh-browser attempts, a screenshot per
failed attempt (DEBUG_DIR, uploaded as the sku-master-debug artifact) and
exits 1 if every attempt failed. Browser helpers shared with the Order Enquiry
job live in common/vinculum_ui.py.

Run locally for testing:
    VIN_LOGIN_ID=Sonali VIN_PASSWORD=... GDRIVE_SERVICE_ACCOUNT_JSON='{...}' \
        python scripts/sku_master_report_to_drive.py
"""

import sys
import os
import time
import json
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.drive_utils import (
    get_drive_service, upload_to_drive, get_latest_downloaded_file, list_file_names,
)
from common.report_dates import should_retry, weekly_file_present
from common.report_dates import drive_filename as drive_filename_for
from common.vinculum_ui import (
    LoginError, dismiss_top_modals, launch_browser, request_dialog_export,
    save_debug_screenshot, select_all_export_fields, vinculum_login,
)

# ── SETTINGS ──────────────────────────────────────────────
LOGIN_ID = os.environ.get("VIN_LOGIN_ID")
PASSWORD = os.environ.get("VIN_PASSWORD")
ORG_ID   = os.environ.get("VIN_ORG_ID", "")

MAX_WAIT_SECONDS = 240   # SKU Master is a large catalog export

GDRIVE_FOLDER_ID = os.environ.get(
    "SKU_MASTER_DRIVE_FOLDER_ID", "1FcWUXpLckpusvBoQUBa66rqQ_b-c0K0o"
)
DRIVE_PREFIX = "SKU Master "

LOCAL_DOWNLOAD_PATH = "/tmp/sku_master_downloads"
DEBUG_DIR = os.environ.get("SKU_MASTER_DEBUG_DIR", "/tmp/sku_master_debug")
REPORT_NAME_MATCH = "SkuEnquiryExport"
BASE = "https://nashermiles.vineretail.com/eRetailWeb"
IST = ZoneInfo("Asia/Kolkata")
MAX_ATTEMPTS = int(os.environ.get("SKU_MASTER_MAX_ATTEMPTS", "2"))
RETRY_PAUSE_SECONDS = 30


class ExportError(Exception):
    """One attempt failed. retryable=False for errors a fresh browser session
    cannot fix."""
    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


def fail(message, retryable=True):
    print(f"\n[✗] {message}")
    raise ExportError(message, retryable)


def validate_env():
    missing = [name for name, val in [
        ("VIN_LOGIN_ID", LOGIN_ID),
        ("VIN_PASSWORD", PASSWORD),
        ("GDRIVE_SERVICE_ACCOUNT_JSON", os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")),
    ] if not val]
    if missing:
        print(f"\n[✗] Missing required environment variables/secrets: {', '.join(missing)}")
        sys.exit(1)


def run_once(drive_service, run_day, attempt):
    """One attempt: fresh browser -> export -> upload. Raises ExportError."""
    print("\n[1] Launching headless browser...")
    page = launch_browser(LOCAL_DOWNLOAD_PATH)
    print("    [OK] Browser launched!")
    try:
        _run_steps(page, drive_service, run_day)
    except Exception:
        save_debug_screenshot(page, DEBUG_DIR, f"fail_{run_day:%Y-%m-%d}_attempt{attempt}.png")
        raise
    finally:
        try:
            page.quit()
        except Exception:
            pass


def _run_steps(page, drive_service, run_day):
    """Steps 2-9 in an already-launched browser."""
    # ── STEP 2: LOGIN ────────────────────────────────────
    print("\n[2] Logging in...")
    try:
        vinculum_login(page, BASE, LOGIN_ID, PASSWORD, ORG_ID)
    except LoginError as e:
        fail(str(e), retryable=False)   # never retried: repeated failed logins can lock the user
    dismiss_top_modals(page, "after login")

    # ── SHARED: locate the frame hosting a marker text ───
    def find_context(marker_text, max_wait=15):
        check_js = f"""
            var els = document.querySelectorAll('a,span,li,button,div,th');
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

    # ── STEP 3: NAVIGATE TO MASTER > SKU MASTER ──────────
    print("\n[3] Navigating to Master > SKU Master...")
    nav_result = page.run_js("""
        function findByText(tag, text) {
            var els = document.querySelectorAll(tag);
            for (var el of els) {
                if ((el.innerText || el.textContent || '').trim() === text) return el;
            }
            return null;
        }
        var direct = findByText('a','SKU Master') || findByText('span','SKU Master') || findByText('li','SKU Master');
        if (direct) { direct.click(); return '[✓] Clicked "SKU Master"'; }
        var master = findByText('a','Master') || findByText('span','Master') || findByText('li','Master');
        if (master) { master.click(); }
        return '[!] "SKU Master" not immediately visible — opened Master menu';
    """)
    print(f"    {nav_result}")

    if "not immediately visible" in nav_result:
        time.sleep(2)
        nav_result2 = page.run_js("""
            function findByText(tag, text) {
                var els = document.querySelectorAll(tag);
                for (var el of els) {
                    if ((el.innerText || el.textContent || '').trim() === text) return el;
                }
                return null;
            }
            var el = findByText('a','SKU Master') || findByText('span','SKU Master') || findByText('li','SKU Master');
            if (el) { el.click(); return '[✓] Clicked "SKU Master" after opening Master menu'; }
            return '[!] Still not found — manual selector fix needed';
        """)
        print(f"    {nav_result2}")

    time.sleep(4)

    # ── STEP 3b: FIND THE SKU ENQUIRY FRAME ──────────────
    print("\n[3b] Locating SKU Master frame context...")
    ctx, ctx_desc = find_context('SKU Code')
    if ctx is None:
        ctx, ctx_desc = find_context('SKU Name')
    if ctx is None:
        ctx, ctx_desc = find_context('Advance Search')
    if ctx is None:
        fail("Could not find the SKU Master grid in the page or any iframe.")
    print(f"    [✓] SKU Master content found in: {ctx_desc}")
    dismiss_top_modals(page, "on SKU Master")

    # ── HELPER: click a visible element by exact text ────
    def js_click_visible_text(texts, tags=('a', 'span', 'li', 'button', 'div'), max_wait=15, context=None):
        c = context or ctx
        texts_json = json.dumps(texts)
        tags_json = json.dumps(tags)
        deadline = time.time() + max_wait
        while time.time() < deadline:
            result = c.run_js(f"""
                var texts = {texts_json};
                var tags = {tags_json};
                for (var t of tags) {{
                    var els = document.querySelectorAll(t);
                    for (var el of els) {{
                        var txt = (el.innerText || el.textContent || '').trim();
                        if (texts.indexOf(txt) !== -1) {{
                            var r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {{ el.click(); return 'clicked:' + txt; }}
                        }}
                    }}
                }}
                return 'not_ready';
            """)
            if result and result.startswith('clicked:'):
                return True, result
            time.sleep(1)
        return False, 'timeout'

    # ── STEP 4: SEARCH + WAIT FOR DATA ────────────────────
    print("\n[4] Clicking Search...")
    ok, info = js_click_visible_text(['Search'], tags=('button', 'a'))
    if ok:
        print(f"    [✓] {info}")
    else:
        fail(f"Could not click Search ({info}).")

    time.sleep(4)
    try:
        while ctx.run_js("return window.jQuery ? jQuery.active : 0") != 0:
            time.sleep(1)
    except Exception:
        pass

    print("    Waiting for SKU data to load...")
    data_ready = False
    for i in range(30):
        rows = ctx.run_js("""
            return (function(){
                var rows = document.querySelectorAll('tr[role="row"], tbody tr, tr');
                var n = 0;
                for (var r of rows){
                    if (r.querySelector && r.querySelector('th')) continue;
                    var cells = r.querySelectorAll('td');
                    if (cells.length < 2) continue;
                    var txt = (r.innerText||'').replace(/\\s+/g,'');
                    if (txt.length > 3) n++;
                }
                return n;
            })();
        """) or 0
        if rows > 0:
            print(f"    [✓] Data present: {rows} row(s) visible.")
            data_ready = True
            break
        time.sleep(1)
    if not data_ready:
        fail("No SKU rows detected after Search — would export empty.")
    time.sleep(2)

    # ── STEP 5: OPEN EXPORT DIALOG ─────────────────────────
    try:
        run_start_epoch = int(ctx.run_js("return Date.now();"))
    except Exception:
        run_start_epoch = int(time.time() * 1000)

    print("\n[5] Clicking Export (toolbar)...")
    ok, info = js_click_visible_text(['Export'], tags=('button', 'a'))
    if ok:
        print(f"    [✓] {info}")
    else:
        fail(f"Could not click toolbar Export ({info}).")
    time.sleep(2)

    # ── STEP 6: CONFIGURE EXPORT DIALOG ───────────────────
    print("\n[6] Configuring export dialog (Normal Export + all fields)...")

    def find_dialog_ctx(max_wait=15):
        check_js = """
            var els = document.querySelectorAll('div,h1,h2,h3,span');
            for (var el of els) {
                if ((el.innerText || el.textContent || '').indexOf('Select Field For Export') !== -1) {
                    var r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return true;
                }
            }
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

    dctx, dctx_desc = find_dialog_ctx()
    if dctx is None:
        fail("Could not locate the export dialog in any context.")
    print(f"    [✓] Export dialog found in: {dctx_desc}")
    time.sleep(1)

    print("[6a] Entering nested export-fields iframe (dynamicFieldDialogIFrame)...")

    inner = None
    deadline = time.time() + 15
    while time.time() < deadline and inner is None:
        try:
            fr = dctx.get_frame('#dynamicFieldDialogIFrame')
            if fr:
                inner = fr
                break
        except Exception:
            pass
        try:
            for f in page.get_frames():
                try:
                    src = f.run_js("return location.href") or ""
                    if "displayBS.action" in src or f.run_js(
                        "return document.body ? document.body.innerText.indexOf('Select Field Name') !== -1 : false"
                    ):
                        inner = f
                        break
                except Exception:
                    continue
        except Exception:
            pass
        if inner is None:
            time.sleep(1)

    if inner is None:
        fail("Could not enter dynamicFieldDialogIFrame.")

    outer_dctx = dctx
    dctx = inner
    print("    [✓] Now operating inside the export-fields iframe.")

    for _ in range(15):
        ready = dctx.run_js("""
            return (function(){
                var t = document.body ? document.body.innerText : '';
                return (t.indexOf('Select Field Name') !== -1) ||
                       (document.querySelectorAll('input[type=checkbox]').length > 0);
            })();
        """)
        if ready:
            break
        time.sleep(1)

    inner_diag = dctx.run_js("""
        return (function(){
            var boxes = document.querySelectorAll('input[type=checkbox]').length;
            var tables = document.querySelectorAll('table').length;
            var trs = document.querySelectorAll('tr').length;
            var sel = document.querySelectorAll('select').length;
            return 'inner checkboxes=' + boxes + ' tables=' + tables +
                   ' trs=' + trs + ' selects=' + sel;
        })();
    """)
    print(f"    [debug] {inner_diag}")

    src_result = dctx.run_js("""
        try {
            var sel = null;
            document.querySelectorAll('select').forEach(function(s){
                var opts = Array.from(s.options).map(function(o){return (o.text||'').trim();});
                if (opts.indexOf('Normal Export') !== -1) sel = s;
            });
            if (!sel) return "NO_SELECT (leaving default)";
            for (var i=0; i<sel.options.length; i++){
                if ((sel.options[i].text||'').trim() === 'Normal Export'){ sel.selectedIndex = i; break; }
            }
            if (window.jQuery) jQuery(sel).trigger('change');
            else sel.dispatchEvent(new Event('change',{bubbles:true}));
            return "OK|" + (sel.options[sel.selectedIndex].text || '').trim();
        } catch (e) { return "ERR|" + e.toString(); }
    """)
    print(f"    Export Type -> {src_result}")
    time.sleep(1)

    dismiss_top_modals(page, "before field selection")

    # Fields: JS clicks inside the iframe, verified (checkboxes AND jqGrid
    # selection), native clicks only as fallback — common/vinculum_ui.py. The
    # old code clicked the header "select all" box and each row by mouse
    # coordinates and ignored the result: the failure mode that lost
    # Order Enquiry days on 2026-10-07/09 (see Errors and their fixes/).
    fields_ok, st = select_all_export_fields(page, dctx)
    if not fields_ok:
        print(f"    Still unchecked: {st.get('unchecked')}")
        fail(f"Fields not fully selected ({st.get('checked')}/{st.get('total')}, jqGrid "
             f"{st.get('jqSelected')}) — refusing to export a partial report.")
    print(f"    [✓] All {st['total']} export fields selected.")
    time.sleep(1)

    print("    Clicking dialog Export...")
    ok, msg = request_dialog_export(dctx, outer_dctx)
    if not ok:
        fail(msg)
    time.sleep(3)

    # ── STEP 7: OPEN PENDING REPORT PAGE ──────────────────
    print("\n[7] Opening Pending Report page...")
    page.get(f"{BASE}/pendingDisplay")
    time.sleep(5)

    def find_pending_ctx(max_wait=15):
        check_js = """
            var rows = document.querySelectorAll('tr[role="row"], tbody tr, tr');
            for (var row of rows) {
                if (row.querySelector('th')) continue;
                var cells = row.querySelectorAll('td');
                if (cells.length < 5) continue;
                var txt = (row.innerText || '').replace(/\\s+/g, '');
                if (txt.length < 4) continue;
                if (txt.indexOf('Export') !== -1 || txt.indexOf('SUCCESS') !== -1 ||
                    txt.indexOf('WIP') !== -1 || txt.indexOf('EXPIRED') !== -1 ||
                    txt.indexOf('Report') !== -1 || /\\d{2}\\/\\d{2}\\/\\d{4}/.test(txt)) {
                    return true;
                }
            }
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
        print("    [!] Could not locate the pending-report grid — using main page.")
        pctx = page
        pctx_desc = 'main page (fallback)'
    print(f"    [✓] Pending grid found in: {pctx_desc}")

    # ── STEP 8: WAIT FOR A NEW SkuEnquiryExport, THEN DOWNLOAD ──
    print(f"\n[8] Waiting for report (max {MAX_WAIT_SECONDS}s)...")

    def get_latest_report(run_start_epoch_ms):
        raw = pctx.run_js(f"""
            try {{
                function parseTs(s){{
                    var m = s.match(/(\\d{{2}})\\/(\\d{{2}})\\/(\\d{{4}})\\s+(\\d{{1,2}}):(\\d{{2}})\\s*(AM|PM)/i);
                    if (!m) return 0;
                    var d=+m[1], mo=+m[2], y=+m[3], hh=+m[4], mm=+m[5], ap=m[6].toUpperCase();
                    if (ap==='PM' && hh<12) hh+=12;
                    if (ap==='AM' && hh===12) hh=0;
                    return new Date(y, mo-1, d, hh, mm, 0).getTime();
                }}
                var rows = document.querySelectorAll('tr[role="row"]');
                var latest = null;
                for (var row of rows) {{
                    if (row.querySelector('th')) continue;
                    var cells = row.querySelectorAll("td");
                    if (cells.length < 11) continue;
                    var rowText = (row.innerText || '').replace(/\\s+/g, '');
                    if (rowText.length < 4) continue;
                    if (cells[4].innerText.trim().indexOf({json.dumps(REPORT_NAME_MATCH)}) === -1) continue;
                    var reqTs = parseTs(cells[2].innerText.trim());
                    if (reqTs && reqTs < ({run_start_epoch_ms} - 60000)) continue;
                    var idNum = parseInt(cells[0].innerText.trim().replace(/,/g,""),10);
                    if (latest == null || idNum > latest.idNum) {{
                        latest = {{idNum: idNum, id: cells[0].innerText.trim(),
                                   fileId: cells[10].innerText.trim(),
                                   status: cells[1].innerText.trim(),
                                   reqTs: reqTs}};
                    }}
                }}
                if (latest) return JSON.stringify(latest);
                return JSON.stringify({{id:"", fileId:"", status:"NOT_FOUND"}});
            }} catch (e) {{ return JSON.stringify({{id:"", fileId:"", status:"ERROR", error:e.toString()}}); }}
        """)
        try:
            return json.loads(raw)
        except Exception:
            return {"id": "", "fileId": "", "status": "ERROR"}

    def wait_for_new_file(folder, since_ts, timeout=90):
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

    print(f"    Run start (browser epoch ms) = {run_start_epoch}")
    time.sleep(5)

    while time.time() - start_time < MAX_WAIT_SECONDS:
        # Full reload (not a soft refresh) each poll, so a dropped session
        # can't silently leave us reading a stale/empty grid.
        page.get(f"{BASE}/pendingDisplay")
        time.sleep(3)

        if "login" in page.url.lower():
            fail("Session expired mid-run (redirected to login) while waiting for the report.")

        pctx, _ = find_pending_ctx(max_wait=5)
        if pctx is None:
            pctx = page

        info = get_latest_report(run_start_epoch)
        report_id = info.get("id", "")
        file_id = info.get("fileId", "")
        status = info.get("status", "")
        elapsed = int(time.time() - start_time)
        print(f"[{elapsed:3d}s] {REPORT_NAME_MATCH} #{report_id}  Status: {status}")

        if status == "SUCCESS":
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
                }} catch (e) {{ return e.toString(); }}
            """)
            print(f"    downloadReport -> {clicked}")
            if clicked == "DOWNLOAD_CALLED":
                downloaded_file = wait_for_new_file(LOCAL_DOWNLOAD_PATH, pre_download_ts, timeout=90)
                if downloaded_file:
                    downloaded = True
                    print(f"    [✓] File downloaded: {downloaded_file}")
                else:
                    print("    [!] downloadReport fired but no file appeared in time.")
            break

        print("    Waiting 10 seconds...")
        time.sleep(10)

    if not downloaded:
        fail(f"Report did not download within {MAX_WAIT_SECONDS}s.")

    # ── STEP 9: UPLOAD TO DRIVE + CLEAN UP ────────────────
    print("\n[9] Uploading to Google Drive...")
    local_file = downloaded_file or get_latest_downloaded_file(LOCAL_DOWNLOAD_PATH, since_ts=pre_download_ts)

    if not local_file:
        fail(f"Could not find the downloaded file in {LOCAL_DOWNLOAD_PATH}.")

    print(f"    [✓] Found file: {local_file}")
    drive_filename = drive_filename_for(run_day, DRIVE_PREFIX)
    uploaded_file_id = upload_to_drive(drive_service, local_file, drive_filename, GDRIVE_FOLDER_ID)

    if not uploaded_file_id:
        fail("Drive upload failed. Check that the folder is shared with the service account's client_email.")

    print(f"    Link: https://drive.google.com/file/d/{uploaded_file_id}/view")
    try:
        os.remove(local_file)
        print(f"    [✓] Local temp file deleted: {local_file}")
    except Exception as e:
        print(f"    [!] Could not delete local temp file: {e}")

    print("\n" + "=" * 55)
    print("  [✓] SUCCESS! SKU Master report uploaded to Google Drive as a Sheet.")
    print(f"  Drive folder: https://drive.google.com/drive/folders/{GDRIVE_FOLDER_ID}")
    print("=" * 55)


def _env_true(name):
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "y")


def main():
    validate_env()
    os.makedirs(LOCAL_DOWNLOAD_PATH, exist_ok=True)

    print("=" * 55)
    print("  Nasher Miles - SKU Master Export -> Google Drive")
    print("=" * 55)

    print("\n[0] Authenticating Google Drive API (service account)...")
    drive_service = get_drive_service()
    print("    [OK] Drive API ready!")

    today = datetime.now(IST).date()
    scheduled = os.environ.get("GITHUB_EVENT_NAME") == "schedule"
    skip_if_present = scheduled or _env_true("SKIP_EXISTING")
    print(f"\n    Mode: {'scheduled (skip if this week already has a file)' if scheduled else 'manual'}"
          f" — target file: {drive_filename_for(today, DRIVE_PREFIX)}")
    if skip_if_present:
        existing = list_file_names(drive_service, GDRIVE_FOLDER_ID, DRIVE_PREFIX.strip())
        if existing is not None and weekly_file_present(today, existing, DRIVE_PREFIX):
            print("\n[✓] Nothing to do — a SKU Master file dated this week is already in Drive.")
            return

    attempt = 1
    while True:
        try:
            run_once(drive_service, today, attempt)
            print(f"\n  {drive_filename_for(today, DRIVE_PREFIX)}: OK (attempt {attempt})")
            return
        except Exception as e:
            retryable = getattr(e, "retryable", True)
            if not isinstance(e, ExportError):
                traceback.print_exc()
            print(f"\n[!] Attempt {attempt}/{MAX_ATTEMPTS} failed: {e}")
            if should_retry(attempt, MAX_ATTEMPTS, retryable):
                print(f"    Retrying with a fresh browser in {RETRY_PAUSE_SECONDS}s...")
                time.sleep(RETRY_PAUSE_SECONDS)
                attempt += 1
                continue
            print(f"\n[✗] SKU Master not exported after {attempt} attempt(s).")
            sys.exit(1)


if __name__ == "__main__":
    main()
