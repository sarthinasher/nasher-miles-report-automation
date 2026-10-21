"""
Nasher Miles eRetail - Order Enquiry Export -> Google Drive Uploader
========================================================================
CI / GitHub Actions version. Headless Chrome, all secrets from env vars,
Drive auth via service account (see common/drive_utils.py).

Login -> Sales -> Order Enquiry -> Order Date = REPORT DATE -> Search ->
Export -> (dialog) Order Source = WMS + Select ALL fields -> Export ->
Pending Report -> wait for a NEW OrderEnquiryExport SUCCESS ->
downloadReport() -> upload to Google Drive as OrderEnquiry_DD-MM-YYYY
(the REPORT date, not the run date).

Which locations (2026-10-19): Vinculum exports only the session's location, so the
report is exported once per location the login can switch to (VIN_LOCATIONS: blank /
all = every one; "A03,NM4"; "all,-S01") and the files are merged into the one Drive
file (common/vinculum_ui.export_all_locations, common/report_merge.py). VIN_DRY_RUN=true
= export + merge without uploading.

Which dates (see common/report_dates.py):
  REPORT_DATE unset/blank -> catch-up: the last 7 days ending yesterday (IST)
                             whose file is NOT yet in Drive (normally just yesterday;
                             a day lost to a failed run is picked up by the next run).
  REPORT_DATE=08/10/2026 | 06/10/2026,08/10/2026 | 06/10/2026-09/10/2026
                          -> exactly those dates (re-exported even if the file exists,
                             unless SKIP_EXISTING=true).
Each date gets up to MAX_ATTEMPTS fresh-browser attempts; the run exits 1 if any
date still failed, so the failure shows in Actions.

Run locally for testing:
    VIN_LOGIN_ID=Sonali VIN_PASSWORD=... GDRIVE_SERVICE_ACCOUNT_JSON='{...}' \
        REPORT_DATE=08/10/2026 python scripts/order_enquiry_report_to_drive.py
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
from common.report_dates import (
    DEFAULT_LOOKBACK_DAYS, ReportDateError, default_window, parse_report_dates, pending_dates,
    should_retry, vinculum_date,
)
from common.report_dates import drive_filename as drive_filename_for
from common.report_merge import (
    count_rows, dry_run, keep_dry_run_copy, merge_for_upload, read_rows, remove_files,
)
from common.vinculum_ui import (
    LOCATIONS_WITH_ORDERS, location_spec,
    SESSION_LOST_HINT, LoginError, dismiss_top_modals, export_all_locations,
    request_dialog_export, select_all_export_fields, session_lost, vinculum_login,
)
from common.vinculum_ui import launch_browser as _launch_browser
from common.vinculum_ui import save_debug_screenshot as _save_debug_screenshot

# ── SETTINGS ──────────────────────────────────────────────
LOGIN_ID = os.environ.get("VIN_LOGIN_ID")
PASSWORD = os.environ.get("VIN_PASSWORD")
ORG_ID   = os.environ.get("VIN_ORG_ID", "")

MAX_WAIT_SECONDS = 180

# NOTE: point this at the real Order Enquiry Drive folder if this placeholder
# ID isn't already correct — and make sure it's shared with the service
# account's client_email as Editor.
GDRIVE_FOLDER_ID = os.environ.get(
    "ORDER_ENQUIRY_DRIVE_FOLDER_ID", "1rinwTyd3BseS7o_FHP4Y907egQz39zBO"
)

LOCAL_DOWNLOAD_PATH = "/tmp/order_enquiry_downloads"
DEBUG_DIR = os.environ.get("ORDER_ENQUIRY_DEBUG_DIR", "/tmp/order_enquiry_debug")
REPORT_NAME_MATCH = "OrderEnquiryExport"
BASE = "https://nashermiles.vineretail.com/eRetailWeb"
# Locations exported when VIN_LOCATIONS is not set (2026-10-21): the 6 that had orders
# on 11-19 Oct (the other 11 had 0 every day).
DEFAULT_LOCATIONS = LOCATIONS_WITH_ORDERS
IST = ZoneInfo("Asia/Kolkata")
MAX_ATTEMPTS = int(os.environ.get("ORDER_ENQUIRY_MAX_ATTEMPTS", "2"))   # per report date
RETRY_PAUSE_SECONDS = 30
EXPORT_GAP_SECONDS = 130     # min time between two export requests in one process (see step 6)
_last_export_request_ts = 0.0
_seen_report_ids = set()     # Pending Report ids already used/seen by an earlier attempt
# {report date: {location: downloaded file or None}} - a retry (fresh browser) does not
# export again the locations an earlier attempt already got.
_location_files = {}


class ExportError(Exception):
    """One attempt for one report date failed. retryable=False for errors a
    fresh browser session cannot fix."""
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


def save_debug_screenshot(page, name):
    _save_debug_screenshot(page, DEBUG_DIR, name)


def launch_browser():
    return _launch_browser(LOCAL_DOWNLOAD_PATH)


def run_once(drive_service, report_day, attempt):
    """One attempt: fresh browser -> export report_day -> upload. Raises ExportError."""
    print("\n[1] Launching headless browser...")
    page = launch_browser()
    print("    [OK] Browser launched!")
    try:
        _run_steps(page, drive_service, report_day)
    except Exception:
        save_debug_screenshot(page, f"fail_{report_day:%Y-%m-%d}_attempt{attempt}.png")
        raise
    finally:
        try:
            page.quit()
        except Exception:
            pass


def _run_steps(page, drive_service, report_day):
    """Steps 2-10 for one report date in an already-launched browser."""
    # ── STEP 2: LOGIN ────────────────────────────────────
    print("\n[2] Logging in...")
    # LoginError (with Vinculum's reason) stops the whole run: see main().
    vinculum_login(page, BASE, LOGIN_ID, PASSWORD, ORG_ID)
    dismiss_top_modals(page, "after login")
    home_url = page.url

    # ── STEPS 3-9 PER LOCATION, THEN MERGE (2026-10-19) ──
    # Vinculum exports only the session's location: export every location the user
    # can switch to (VIN_LOCATIONS) and merge them into the one OrderEnquiry_ file.
    print("\n[2b] Locations...")
    drive_filename = drive_filename_for(report_day)
    parts, local_file = [], None
    try:
        parts = export_all_locations(
            page, BASE, lambda loc: _export_location(page, report_day, loc, home_url),
            done=_location_files.setdefault(report_day, {}),
            spec=location_spec(DEFAULT_LOCATIONS))
        local_file, _total = merge_for_upload(parts, LOCAL_DOWNLOAD_PATH, drive_filename,
                                              required_columns=OE_REQUIRED_COLUMNS)
        if not local_file:
            fail(f"No location returned an Order Enquiry file for {vinculum_date(report_day)}.")
        if dry_run():
            keep_dry_run_copy(local_file, DEBUG_DIR, drive_filename)
            return

        # ── STEP 10: UPLOAD TO DRIVE ─────────────────────
        print("\n[10] Uploading to Google Drive...")
        uploaded_file_id = upload_to_drive(drive_service, local_file, drive_filename, GDRIVE_FOLDER_ID)
        if not uploaded_file_id:
            fail("Drive upload failed. Check that the folder is shared with the service account's client_email.")
        print(f"    Link: https://drive.google.com/file/d/{uploaded_file_id}/view")
    finally:
        remove_files([p for _, p in parts] + [local_file])

    print("\n" + "=" * 55)
    print("  [✓] SUCCESS! Order Enquiry report uploaded to Google Drive as a Sheet.")
    print(f"  Drive folder: https://drive.google.com/drive/folders/{GDRIVE_FOLDER_ID}")
    print("=" * 55)


# Order Enquiry result grid: jqGrid's total record count, or null if unreadable.
GRID_RECORDS_JS = """
    try {
        if (!window.jQuery || !jQuery.fn.jqGrid) return null;
        var g = jQuery('table.ui-jqgrid-btable').filter(function(){ return this.offsetWidth > 0; }).first();
        if (!g.length) return null;
        var n = g.jqGrid('getGridParam', 'records');
        return (typeof n === 'number') ? n : (n === undefined || n === null || n === '' ? null : parseInt(n, 10));
    } catch (e) { return null; }
"""

ORG_LOC_JS = "var e = document.getElementById('orgLocKey'); return e ? e.value : '';"

# What nasher-fms-automation's Order Enquiry reader needs ("Extern OrderNo" -> ExtOrderNo).
OE_REQUIRED_COLUMNS = ["Order No", "Extern OrderNo", "Order Date", "Status"]


def _export_location(page, report_day, loc, home_url):
    """Steps 3-9 for the session's current location (loc): returns the downloaded
    file, or None when the location has no orders that day."""
    # Each location starts from the landing page (the Sales > Order Enquiry menu
    # lives there); its hidden orgLocKey ("NM4_NASA") proves the switch took.
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
        fail(f"Logged out of Vinculum: {SESSION_LOST_HINT}.")
    if org_loc and not org_loc.startswith(loc.upper() + "_"):
        fail(f"Landing page is on {org_loc}, not {loc} - the location switch did not take.")

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

    # ── STEP 3: NAVIGATE TO SALES > ORDER ENQUIRY ────────
    print("\n[3] Navigating to Sales > Order Enquiry...")
    nav_result = page.run_js("""
        function findByText(tag, text) {
            var els = document.querySelectorAll(tag);
            for (var el of els) {
                if ((el.innerText || el.textContent || '').trim() === text) return el;
            }
            return null;
        }
        var direct = findByText('a','Order Enquiry') || findByText('span','Order Enquiry') || findByText('li','Order Enquiry');
        if (direct) { direct.click(); return '[✓] Clicked "Order Enquiry"'; }
        var sales = findByText('a','Sales') || findByText('span','Sales') || findByText('li','Sales');
        if (sales) { sales.click(); }
        return '[!] "Order Enquiry" not immediately visible — opened Sales menu';
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
            var el = findByText('a','Order Enquiry') || findByText('span','Order Enquiry') || findByText('li','Order Enquiry');
            if (el) { el.click(); return '[✓] Clicked "Order Enquiry" after opening Sales menu'; }
            return '[!] Still not found — manual selector fix needed';
        """)
        print(f"    {nav_result2}")

    time.sleep(4)

    # ── STEP 3b: FIND THE ORDER ENQUIRY FRAME ────────────
    print("\n[3b] Locating Order Enquiry frame context...")
    ctx, ctx_desc = find_context('Ext Order No')
    if ctx is None:
        ctx, ctx_desc = find_context('Advance Search')
    if ctx is None:
        fail("Could not find the Order Enquiry grid in the page or any iframe.")
    print(f"    [✓] Order Enquiry content found in: {ctx_desc}")
    dismiss_top_modals(page, "on Order Enquiry")

    # ── STEP 4: SET ORDER DATE = YESTERDAY ───────────────
    y_str = vinculum_date(report_day)
    print(f"\n[4] Setting Order Date to {y_str}...")

    date_result = ctx.run_js(f"""
        try {{
            if (!window.jQuery) return "NO_JQUERY";
            var $ = window.jQuery;
            var target = null;
            $('input').each(function() {{
                var drp = $(this).data('daterangepicker');
                if (drp && ((this.value || '').indexOf('/') !== -1)) {{ target = this; return false; }}
            }});
            if (!target) return "NO_DATE_INPUT";
            var drp = $(target).data('daterangepicker');
            drp.setStartDate(moment("{y_str}", "DD/MM/YYYY").startOf('day'));
            drp.setEndDate(moment("{y_str}", "DD/MM/YYYY").endOf('day'));
            $(target).trigger('apply.daterangepicker', drp);
            return "OK|" + target.value;
        }} catch (e) {{ return "ERR|" + e.toString(); }}
    """)
    print(f"    daterangepicker -> {date_result}")
    if not (date_result or "").startswith("OK"):
        fail(f"Could not set Order Date automatically to {y_str}.")
    time.sleep(1)

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

    # ── STEP 5: SEARCH ────────────────────────────────────
    print("\n[5] Clicking Search...")
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
    time.sleep(2)
    try:
        n_orders = ctx.run_js(GRID_RECORDS_JS)
    except Exception:
        n_orders = None
    print(f"    {loc}: Order Enquiry grid shows {n_orders} order(s) for {y_str}")
    if n_orders == 0:                     # re-check once: the grid may still be filling
        time.sleep(5)
        try:
            n_orders = ctx.run_js(GRID_RECORDS_JS)
        except Exception:
            n_orders = None
        if n_orders == 0:
            print(f"    [✓] {loc}: no orders for {y_str} - nothing to export here.")
            return None

    # ── STEP 6: OPEN EXPORT DIALOG ────────────────────────
    # The Pending Report grid only shows request times to the minute, and step 9
    # accepts any OrderEnquiryExport requested from 60s before run start. When
    # one process exports several dates (back-fill / retry), keep requests
    # >= EXPORT_GAP_SECONDS apart so the previous date's file can never be
    # picked up as this date's.
    global _last_export_request_ts
    gap = EXPORT_GAP_SECONDS - (time.time() - _last_export_request_ts)
    if gap > 0:
        print(f"\n    Waiting {int(gap)}s so this export can't be confused with the previous one...")
        time.sleep(gap)
    try:
        run_start_epoch = int(ctx.run_js("return Date.now();"))
    except Exception:
        run_start_epoch = int(time.time() * 1000)

    print("\n[6] Clicking Export (toolbar)...")
    ok, info = js_click_visible_text(['Export'], tags=('button', 'a'))
    if ok:
        print(f"    [✓] {info}")
    else:
        fail(f"Could not click toolbar Export ({info}).")
    time.sleep(2)

    # ── STEP 7: CONFIGURE EXPORT DIALOG ───────────────────
    print("\n[7] Configuring export dialog (WMS + all fields)...")

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

    print("[7a] Entering nested export-fields iframe (dynamicFieldDialogIFrame)...")

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
                if (opts.indexOf('WMS') !== -1) sel = s;
            });
            if (!sel) return "NO_SELECT";
            for (var i=0; i<sel.options.length; i++){
                if ((sel.options[i].text||'').trim() === 'WMS'){ sel.selectedIndex = i; break; }
            }
            if (window.jQuery) jQuery(sel).trigger('change');
            else sel.dispatchEvent(new Event('change',{bubbles:true}));
            return "OK|" + (sel.options[sel.selectedIndex].text || '').trim();
        } catch (e) { return "ERR|" + e.toString(); }
    """)
    print(f"    Order Source -> {src_result}")
    time.sleep(1)
    dismiss_top_modals(page, "before field selection")

    # Field grid = jqGrid "dynamicFieldGrid" (multiselect, 39 rows; Order No /
    # Extern OrderNo / Order Date come pre-selected and locked). Selected with
    # JS clicks inside the iframe and verified (checkboxes AND jqGrid
    # selection); native clicks only as fallback — see common/vinculum_ui.py.
    # The old code clicked by mouse coordinates and ignored the result — when
    # those clicks did not land (2026-10-07/09) every try stayed at 3/39.
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

    _last_export_request_ts = time.time()
    time.sleep(3)

    # ── STEP 8: OPEN PENDING REPORT PAGE ──────────────────
    print("\n[8] Opening Pending Report page...")
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

    # ── STEP 9: WAIT FOR A NEW OrderEnquiryExport, THEN DOWNLOAD ─
    print(f"\n[9] Waiting for report (max {MAX_WAIT_SECONDS}s)...")

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
        if session_lost(page):
            fail(f"Logged out of Vinculum while waiting for the report: {SESSION_LOST_HINT}.")
        pctx, _ = find_pending_ctx(max_wait=5)
        if pctx is None:
            pctx = page

        info = get_latest_report(run_start_epoch)
        report_id = info.get("id", "")
        file_id = info.get("fileId", "")
        status = info.get("status", "")
        elapsed = int(time.time() - start_time)
        if report_id and report_id in _seen_report_ids:
            status = f"(#{report_id} belongs to an earlier date/attempt — waiting for this one)"
        print(f"[{elapsed:3d}s] {REPORT_NAME_MATCH} #{report_id}  Status: {status}")

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
                }} catch (e) {{ return e.toString(); }}
            """)
            print(f"    downloadReport -> {clicked}")
            if clicked == "DOWNLOAD_CALLED":
                downloaded_file = wait_for_new_file(LOCAL_DOWNLOAD_PATH, pre_download_ts, timeout=90)
                if downloaded_file:
                    downloaded = True
                    _last_export_request_ts = 0.0   # consumed: no gap needed before the next
                    print(f"    [✓] File downloaded: {downloaded_file}")
                else:
                    print("    [!] downloadReport fired but no file appeared in time.")
            break

        print("    Waiting 10 seconds...")
        time.sleep(10)
        page.refresh()
        time.sleep(3)

    if not downloaded:
        fail(f"Report did not download within {MAX_WAIT_SECONDS}s.")

    local_file = downloaded_file or get_latest_downloaded_file(LOCAL_DOWNLOAD_PATH, since_ts=pre_download_ts)
    if not local_file:
        fail(f"Could not find the downloaded file in {LOCAL_DOWNLOAD_PATH}.")
    print(f"    [✓] Found file: {local_file}")
    # The export must hold every order the grid showed (2026-10-21: misread files
    # with 0 rows were merged and uploaded).
    try:
        n_file = count_rows(read_rows(local_file)[1])
    except Exception as e:
        fail(f"{loc}: could not read the exported file ({e!r}).")
    if isinstance(n_orders, (int, float)) and n_file < n_orders:
        fail(f"{loc}: exported file has {n_file} order row(s) but the grid showed {n_orders}.")
    return local_file


def existing_drive_names(drive_service, folder_id, prefix="OrderEnquiry_"):
    """Names of the non-trashed files in the folder starting with prefix.
    Returns None if Drive can't be listed (caller then exports everything)."""
    return list_file_names(drive_service, folder_id, prefix)


def _env_true(name):
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "y")


def main():
    validate_env()
    os.makedirs(LOCAL_DOWNLOAD_PATH, exist_ok=True)

    print("=" * 55)
    print("  Nasher Miles - Order Enquiry Export -> Google Drive")
    print("=" * 55)

    today = datetime.now(IST).date()
    spec = os.environ.get("REPORT_DATE", "")
    try:
        requested = parse_report_dates(spec, today)
    except ReportDateError as e:
        print(f"\n[✗] REPORT_DATE: {e}")
        sys.exit(2)

    if requested:
        days, skip_existing = requested, _env_true("SKIP_EXISTING")
        mode = f"requested dates ({spec.strip()})"
    else:
        days, skip_existing = default_window(today), True
        mode = f"catch-up window (last {DEFAULT_LOOKBACK_DAYS} days ending yesterday, IST)"

    print("\n[0] Authenticating Google Drive API (service account)...")
    drive_service = get_drive_service()
    print("    [OK] Drive API ready!")

    print(f"\n    Mode: {mode}")
    print(f"    Candidate dates: {', '.join(vinculum_date(d) for d in days)}")
    if skip_existing:
        existing = existing_drive_names(drive_service, GDRIVE_FOLDER_ID)
        if existing is not None:
            have = [d for d in days if d not in pending_dates(days, existing)]
            if have:
                print(f"    Already in Drive (skipped): {', '.join(drive_filename_for(d) for d in have)}")
            days = pending_dates(days, existing)
    if not days:
        print("\n[✓] Nothing to do — every candidate date already has its file in Drive.")
        return
    print(f"    Exporting: {', '.join(vinculum_date(d) for d in days)} "
          f"(up to {MAX_ATTEMPTS} attempts each)")

    results = {}
    login_failed = None
    for day in days:
        if login_failed:
            results[day] = "FAILED: not run (Vinculum login failed)"
            continue
        print("\n" + "#" * 55)
        print(f"#  Report date {vinculum_date(day)} -> {drive_filename_for(day)}")
        print("#" * 55)
        attempt = 1
        while True:
            try:
                run_once(drive_service, day, attempt)
                results[day] = f"OK (attempt {attempt})"
                break
            except LoginError as e:
                # Not retried, other dates not tried: repeated failed logins can lock the user.
                print(f"\n[✗] {e}")
                results[day] = f"FAILED: {e}"
                login_failed = e
                break
            except Exception as e:
                retryable = getattr(e, "retryable", True)
                if not isinstance(e, ExportError):
                    traceback.print_exc()
                print(f"\n[!] {vinculum_date(day)} attempt {attempt}/{MAX_ATTEMPTS} failed: {e}")
                if should_retry(attempt, MAX_ATTEMPTS, retryable):
                    print(f"    Retrying with a fresh browser in {RETRY_PAUSE_SECONDS}s...")
                    time.sleep(RETRY_PAUSE_SECONDS)
                    attempt += 1
                    continue
                results[day] = f"FAILED after {attempt} attempt(s): {e}"
                break

    print("\n" + "=" * 55)
    print("  SUMMARY")
    for day, res in results.items():
        print(f"  {drive_filename_for(day)}: {res}")
    print("=" * 55)
    failed = [d for d, r in results.items() if r.startswith("FAILED")]
    if failed:
        print(f"\n[✗] {len(failed)} date(s) not exported: "
              f"{', '.join(vinculum_date(d) for d in failed)} — re-run with report_date to back-fill.")
        sys.exit(1)


if __name__ == "__main__":
    main()
