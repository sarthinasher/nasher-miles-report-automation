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

Which dates (see common/report_dates.py):
  REPORT_DATE unset/blank -> catch-up: the last 3 days ending yesterday (IST)
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

from DrissionPage import ChromiumPage, ChromiumOptions

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.drive_utils import get_drive_service, upload_to_drive, get_latest_downloaded_file
from common.report_dates import (
    ReportDateError, default_window, parse_report_dates, pending_dates,
    should_retry, vinculum_date,
)
from common.report_dates import drive_filename as drive_filename_for

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
IST = ZoneInfo("Asia/Kolkata")
MAX_ATTEMPTS = int(os.environ.get("ORDER_ENQUIRY_MAX_ATTEMPTS", "2"))   # per report date
RETRY_PAUSE_SECONDS = 30
EXPORT_GAP_SECONDS = 130     # min time between two export requests in one process (see step 6)
_last_export_request_ts = 0.0
_seen_report_ids = set()     # Pending Report ids already used/seen by an earlier attempt


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


# Vinculum's top-level page carries hidden Bootstrap modals (z-index 1050 + a
# full-page backdrop) that it opens on its own: #screenNotificationDialog
# ("Remind me after 1 Hour ... / don't show this notification again") and
# #feedbackFormDialog ("Take the Survey / Not Now"), among others. While one is
# open, every coordinate (mouse-event) click lands on its backdrop, but JS
# clicks still work — the suspected trigger of the 2026-10-07/09 "3/39 fields"
# failure (see Errors and their fixes/2026-10-10-*). Close any open
# one in the TOP document only (the export dialog lives inside an iframe and is
# never touched by this). NOTE: which popup was open on the failing runs was not
# captured (not reproduced in 6 diagnostic runs); this is defence in depth — the
# field selection below no longer depends on coordinate clicks at all.
_DISMISS_TOP_MODALS_JS = """
    return (function(){
        var closed = [];
        document.querySelectorAll('.modal').forEach(function(m){
            var st = window.getComputedStyle(m);
            var r = m.getBoundingClientRect();
            if (st.display === 'none' || st.visibility === 'hidden' || !(r.width > 0 && r.height > 0)) return;
            var title = (m.querySelector('.modal-title') || {}).textContent || '';
            closed.push((m.id || m.className) + (title ? ' "' + title.trim().slice(0, 60) + '"' : ''));
            try { if (window.jQuery && jQuery.fn.modal) jQuery(m).modal('hide'); } catch (e) {}
            m.style.display = 'none';
            m.classList.remove('in');
        });
        var backdrops = document.querySelectorAll('.modal-backdrop');
        backdrops.forEach(function(b){ b.parentNode && b.parentNode.removeChild(b); });
        if (closed.length) document.body.classList.remove('modal-open');
        return JSON.stringify({closed: closed, backdrops: backdrops.length});
    })();
"""


def dismiss_top_modals(page, where):
    """Closes any Vinculum popup open in the top document; logs what it closed."""
    try:
        res = json.loads(page.run_js(_DISMISS_TOP_MODALS_JS) or "{}")
    except Exception as e:
        print(f"    [!] popup check failed ({where}): {e!r}")
        return []
    if res.get("closed") or res.get("backdrops"):
        print(f"    [!] Closed Vinculum popup(s) {where}: {res.get('closed')} "
              f"(backdrops removed: {res.get('backdrops')})")
    return res.get("closed") or []


def save_debug_screenshot(page, name):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        page.get_screenshot(path=DEBUG_DIR, name=name, full_page=False)
        print(f"    [debug] screenshot saved: {DEBUG_DIR}/{name}")
    except Exception as e:
        print(f"    [debug] screenshot failed: {e!r}")


def launch_browser():
    co = ChromiumOptions()
    co.headless(True)
    co.auto_port()   # fresh port + profile per attempt: a retry never reuses a dead session
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-gpu")
    co.set_argument("--window-size=1920,1080")
    page = ChromiumPage(addr_or_opts=co)
    page.set.download_path(LOCAL_DOWNLOAD_PATH)
    return page


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
        fail("Login failed — check VIN_LOGIN_ID / VIN_PASSWORD secrets.")
    print("[✓] Login successful!")
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

    _DIALOG_BOXES_JS = """
        return (function(){
        function textOf(n){ return (n.innerText || n.textContent || '').trim(); }

        function allBoxes(){
            return Array.prototype.slice.call(document.querySelectorAll('input[type=checkbox]'))
                .filter(function(b){
                    var st = window.getComputedStyle(b);
                    return !(st && (st.display === 'none' || st.visibility === 'hidden'));
                });
        }

        function headerBox(){
            var th = document.querySelector('th input[type=checkbox]');
            if (th) return th;
            var boxes = allBoxes();
            for (var i=0;i<boxes.length;i++){
                var n = boxes[i];
                for (var d=0; d<5 && n; d++){
                    if (n.tagName === 'TH' || /jqgfirstrow|ui-jqgrid-htable|hdrbox/i.test(n.className||'')) return boxes[i];
                    n = n.parentElement;
                }
            }
            return null;
        }
        function fieldBoxes(){
            var hb = headerBox();
            return allBoxes().filter(function(b){ return b !== hb; });
        }

        function rowNameOf(b){
            var n = b;
            for (var d=0; d<6 && n; d++){
                var t = textOf(n).replace(/\\s+/g,' ');
                if (t.length > 0 && t.length < 40) return t;
                n = n.parentElement;
            }
            return '?';
        }
        function clickBox(b){
            if (!b.checked) {
                b.click();
                if (window.jQuery) jQuery(b).trigger('click').trigger('change');
                else b.dispatchEvent(new Event('change',{bubbles:true}));
            }
        }

        function structureOK(){
            var hasSelect = document.querySelectorAll('select').length > 0;
            var hasTable  = document.querySelectorAll('table').length > 0;
            var boxes     = fieldBoxes().length;
            return hasSelect && hasTable && boxes > 0;
        }
    """
    _DIALOG_BOXES_JS_END = "})();"

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
    # Extern OrderNo / Order Date come pre-selected and locked). Rows are
    # selected with element.click() run IN the iframe: it fires jqGrid's own
    # row-checkbox handler, so it works whatever is drawn on top of the page,
    # and for rows scrolled outside the 380px-high iframe. The old code clicked
    # by mouse coordinates (header "select all" toggle, then each row) and
    # ignored the result — when those clicks did not land (2026-10-07/09) every
    # try stayed at the 3 locked fields. The header checkbox is a TOGGLE, so it
    # is never clicked (clicking it when all are selected would clear them).
    def read_field_state():
        raw = dctx.run_js(_DIALOG_BOXES_JS + """
            if (!structureOK()) return JSON.stringify({total:0, checked:0, bad:'NO_GRID'});
            var fb = fieldBoxes();
            var off = fb.filter(function(b){ return !b.checked; }).map(rowNameOf);
            var sel = null, rows = null;
            try {
                if (window.jQuery && jQuery.fn.jqGrid) {
                    var g = jQuery('table.ui-jqgrid-btable').first();
                    if (g.length && g.jqGrid('getGridParam', 'multiselect')) {
                        sel = (g.jqGrid('getGridParam', 'selarrrow') || []).length;
                        rows = g.jqGrid('getGridParam', 'reccount');
                    }
                }
            } catch (e) {}
            return JSON.stringify({total: fb.length, checked: fb.length - off.length,
                                   unchecked: off, jqSelected: sel, jqRows: rows, bad: null});
        """ + _DIALOG_BOXES_JS_END)
        try:
            return json.loads(raw)
        except Exception:
            return {"total": 0, "checked": 0, "bad": "PARSE", "unchecked": []}

    def all_fields_selected(st):
        if st.get("bad") or not st.get("total"):
            return False
        if st["checked"] != st["total"]:
            return False
        # jqGrid's own selection list is what the export reads — must agree too
        if st.get("jqSelected") is not None and st["jqSelected"] != st["total"]:
            return False
        return True

    def js_click_unchecked():
        raw = dctx.run_js(_DIALOG_BOXES_JS + """
            if (!structureOK()) return JSON.stringify({bad:'NO_GRID', clicked:0});
            var n = 0;
            fieldBoxes().forEach(function(b){
                if (!b.checked && !b.disabled) { b.click(); n++; }
            });
            return JSON.stringify({bad:null, clicked:n});
        """ + _DIALOG_BOXES_JS_END)
        try:
            return json.loads(raw)
        except Exception:
            return {"bad": "PARSE", "clicked": 0}

    def native_click_unchecked():
        """Fallback only: mouse-event click on each still-unchecked row box.
        Returns how many clicks DrissionPage reports as dispatched."""
        ids = dctx.run_js(_DIALOG_BOXES_JS + """
            var out = [];
            fieldBoxes().forEach(function(b, i){
                if (!b.checked && !b.disabled) { if (!b.id) b.id = 'AUTO_FLD_' + i; out.push(b.id); }
            });
            return JSON.stringify(out);
        """ + _DIALOG_BOXES_JS_END)
        done = 0
        for box_id in json.loads(ids or "[]"):
            try:
                el = dctx.ele(f"#{box_id}", timeout=3)
                if el and el.click(by_js=False):
                    done += 1
            except Exception as e:
                print(f"      native click #{box_id} failed: {e!r}")
            time.sleep(0.15)
        return done

    fields_ok = False
    st = read_field_state()
    print(f"    Fields before: {st.get('checked')}/{st.get('total')} checked"
          f" (jqGrid selected {st.get('jqSelected')}/{st.get('jqRows')})")
    for attempt in range(1, 4):
        if all_fields_selected(st):
            fields_ok = True
            break
        if st.get("bad"):
            print(f"    [!] Field grid not ready ({st['bad']}) — waiting...")
            time.sleep(1.5)
            st = read_field_state()
            continue
        res = js_click_unchecked()
        time.sleep(1)
        st = read_field_state()
        print(f"    Select fields (try {attempt}) JS-click {res.get('clicked')} boxes -> "
              f"{st.get('checked')}/{st.get('total')} checked, jqGrid selected "
              f"{st.get('jqSelected')}/{st.get('jqRows')}")
        if all_fields_selected(st):
            fields_ok = True
            break
        n = native_click_unchecked()
        time.sleep(1)
        st = read_field_state()
        print(f"    Select fields (try {attempt}) native-click fallback {n} boxes -> "
              f"{st.get('checked')}/{st.get('total')} checked")
        if all_fields_selected(st):
            fields_ok = True
            break
        dismiss_top_modals(page, "before re-trying field selection")

    if not fields_ok:
        print(f"    Still unchecked: {st.get('unchecked')}")
        fail(f"Fields not fully selected ({st.get('checked')}/{st.get('total')}, jqGrid "
             f"{st.get('jqSelected')}) — refusing to export a partial report.")
    print(f"    [✓] All {st['total']} export fields selected.")
    time.sleep(1)

    print("    Clicking dialog Export...")

    _EXPORT_FINDER_JS = """
        return (function(){
            function inDialog(el){
                var n = el;
                for (var i=0; i<15 && n; i++){
                    var t = (n.innerText || '');
                    if (t.indexOf('Select Field') !== -1 || t.indexOf('Export') !== -1) return true;
                    n = n.parentElement;
                }
                return false;
            }
            var btns = document.querySelectorAll('button, a, input[type=button], input[type=submit]');
            var byOnclick = null, byText = null;
            for (var b of btns){
                var txt = (b.innerText || b.textContent || b.value || '').trim();
                var oc  = (b.getAttribute('onclick') || '');
                var r   = b.getBoundingClientRect();
                if (!(r.width > 0 && r.height > 0)) continue;
                if (oc.indexOf('dynamicExport') !== -1) { byOnclick = b; break; }
                if (txt === 'Export' && !byText) byText = b;
            }
            var target = byOnclick || byText;
            if (!target) return "NO_DIALOG_EXPORT";
            if (!target.id) target.id = 'AUTO_DLG_EXPORT';
            return "FOUND|" + target.id + "|" + (target.getAttribute('onclick') || '');
        })();
    """

    export_ctx = None
    tag_result = "NO_DIALOG_EXPORT"
    for label, cxt in (("inner iframe", dctx), ("outer modal", outer_dctx)):
        res = cxt.run_js(_EXPORT_FINDER_JS)
        print(f"    export button search ({label}) -> {res}")
        if (res or "").startswith("FOUND"):
            export_ctx = cxt
            tag_result = res
            break

    if not (tag_result or "").startswith("FOUND"):
        fail("Could not isolate the dialog Export button.")

    btn_id = tag_result.split("|")[1]

    _DIALOG_OPEN_JS = """
        var els = document.querySelectorAll('div,h1,h2,h3,span');
        for (var el of els){
            if ((el.innerText || '').indexOf('Select Field For Export') !== -1){
                var r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) return true;
            }
        }
        return false;
    """

    def dialog_open():
        try:
            return bool(outer_dctx.run_js(_DIALOG_OPEN_JS))
        except Exception:
            return False

    dlg_clicked = False
    try:
        ebtn = export_ctx.ele(f"#{btn_id}", timeout=6)
        if ebtn and ebtn.click(by_js=False):
            dlg_clicked = True
            print("    [✓] Native click on dialog Export.")
    except Exception as e:
        print(f"    Native dialog click failed: {e}")

    time.sleep(2)
    still_open = dialog_open()
    print(f"    Dialog still open after click? {still_open}")
    if still_open:
        # The native click did not land (same failure class as the field
        # checkboxes) — fire the button's own click handler instead.
        res = export_ctx.run_js(
            f"var b = document.getElementById({json.dumps(btn_id)});"
            " if (!b) return 'NO_BUTTON'; b.click(); return 'JS_CLICKED';")
        print(f"    Dialog Export JS-click fallback -> {res}")
        dlg_clicked = dlg_clicked or res == "JS_CLICKED"
        time.sleep(3)
        still_open = dialog_open()
        print(f"    Dialog still open after JS click? {still_open}")
        if still_open:
            fail("Export dialog did not close after clicking Export — export not requested.")

    if not dlg_clicked:
        fail("Could not click dialog Export.")
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

    # ── STEP 10: UPLOAD TO DRIVE + CLEAN UP ───────────────
    print("\n[10] Uploading to Google Drive...")
    local_file = downloaded_file or get_latest_downloaded_file(LOCAL_DOWNLOAD_PATH, since_ts=pre_download_ts)

    if not local_file:
        fail(f"Could not find the downloaded file in {LOCAL_DOWNLOAD_PATH}.")

    print(f"    [✓] Found file: {local_file}")
    drive_filename = drive_filename_for(report_day)
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
    print("  [✓] SUCCESS! Order Enquiry report uploaded to Google Drive as a Sheet.")
    print(f"  Drive folder: https://drive.google.com/drive/folders/{GDRIVE_FOLDER_ID}")
    print("=" * 55)


def existing_drive_names(drive_service, folder_id, prefix="OrderEnquiry_"):
    """Names of the non-trashed files in the folder starting with prefix.
    Returns None if Drive can't be listed (caller then exports everything)."""
    names, token = set(), None
    try:
        while True:
            resp = drive_service.files().list(
                q=f"'{folder_id}' in parents and trashed=false and name contains '{prefix}'",
                fields="nextPageToken, files(name)", pageSize=1000, pageToken=token,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            names.update(f["name"] for f in resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                return names
    except Exception as e:
        print(f"    [!] Could not list Drive folder ({e!r}) — not skipping any date.")
        return None


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
        mode = "catch-up window (last 3 days ending yesterday, IST)"

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
    for day in days:
        print("\n" + "#" * 55)
        print(f"#  Report date {vinculum_date(day)} -> {drive_filename_for(day)}")
        print("#" * 55)
        attempt = 1
        while True:
            try:
                run_once(drive_service, day, attempt)
                results[day] = f"OK (attempt {attempt})"
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
