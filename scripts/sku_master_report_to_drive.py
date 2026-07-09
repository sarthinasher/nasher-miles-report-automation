"""
Nasher Miles eRetail - SKU Master Export -> Google Drive Uploader
==================================================================
CI / GitHub Actions version. Headless Chrome, all secrets from env vars,
Drive auth via service account (see common/drive_utils.py).

Logs in -> Master -> SKU Master -> Search -> (wait for data) -> Export ->
(dialog) Export Type = Normal Export + Select ALL fields -> Export ->
Pending Report -> waits for a NEW SkuEnquiryExport SUCCESS -> downloads via
downloadReport() -> uploads to Drive as a native Google Sheet -> deletes the
local temp copy.

Env vars required:
    VIN_LOGIN_ID
    VIN_PASSWORD
    GDRIVE_SERVICE_ACCOUNT_JSON   (full JSON, handled inside common/drive_utils.py)
"""

import os
import sys
import glob
import json
import time

from DrissionPage import ChromiumPage, ChromiumOptions

# Shared Drive auth + upload (service account, retry on 5xx) used by every
# script in this repo. See common/drive_utils.py.
from common.drive_utils import get_drive_service, upload_to_drive


# ── SETTINGS ─────────────────────────────────────────────
LOGIN_ID = os.environ.get("VIN_LOGIN_ID", "")
PASSWORD = os.environ.get("VIN_PASSWORD", "")
ORG_ID   = os.environ.get("VIN_ORG_ID", "")   # optional

MAX_WAIT_SECONDS = 240   # SKU Master is a large catalog export

GDRIVE_FOLDER_ID = "1FcWUXpLckpusvBoQUBa66rqQ_b-c0K0o"

REPORT_NAME_MATCH = "SkuEnquiryExport"

# Repo-relative staging folder (cleaned up by .gitignore's *_downloads/ rule).
LOCAL_DOWNLOAD_PATH = os.path.join(os.getcwd(), "temp_downloads", "SKUMaster")

BASE = "https://nashermiles.vineretail.com/eRetailWeb"
# ────────────────────────────────────────────────────────


def fail(msg):
    """Print an error and exit non-zero so the Actions run is marked failed."""
    print(f"\n[!] {msg}")
    try:
        page.quit()
    except Exception:
        pass
    sys.exit(1)


if not LOGIN_ID or not PASSWORD:
    print("[!] VIN_LOGIN_ID / VIN_PASSWORD not set in environment.")
    sys.exit(1)

os.makedirs(LOCAL_DOWNLOAD_PATH, exist_ok=True)

print("=" * 55)
print("  Nasher Miles - SKU Master Export Downloader (CI)")
print("=" * 55)


# ── HEADLESS CHROME ──────────────────────────────────────
co = ChromiumOptions()
co.headless(True)
co.set_argument("--no-sandbox")
co.set_argument("--disable-dev-shm-usage")
co.set_argument("--disable-gpu")
co.set_argument("--window-size=1920,1080")
co.set_pref("download.default_directory", LOCAL_DOWNLOAD_PATH)

page = ChromiumPage(co)
page.set.download_path(LOCAL_DOWNLOAD_PATH)


# ── DRIVE AUTH (service account) ─────────────────────────
print("\n[0] Authenticating Google Drive API (service account)...")
try:
    drive_service = get_drive_service()
except Exception as e:
    fail(f"Drive auth failed: {e}")
print("    [OK] Drive API ready!")


def get_latest_downloaded_file(folder, extensions=(".xlsx", ".xls", ".csv"), since_ts=None):
    candidates = []
    for ext in extensions:
        candidates.extend(glob.glob(os.path.join(folder, f"*{ext}")))
    if since_ts is not None:
        candidates = [f for f in candidates if os.path.getmtime(f) >= since_ts]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


# ── STEP 1: LOGIN ────────────────────────────────────────
print("\n[1] Logging in...")
page.get(f"{BASE}/eRetailLogin.action?popup=true")
time.sleep(4)

page.run_js("document.querySelector('[name=userOrgId]').removeAttribute('readonly')")
page.run_js("document.querySelector('[name=userName]').removeAttribute('readonly')")
page.run_js("document.querySelector('[id=password]').removeAttribute('readonly')")
time.sleep(1)

if ORG_ID:
    page.run_js(f"document.querySelector('[name=userOrgId]').value = {json.dumps(ORG_ID)}")
page.run_js(f"document.querySelector('[name=userName]').value = {json.dumps(LOGIN_ID)}")
page.run_js(f"document.querySelector('[id=password]').value = {json.dumps(PASSWORD)}")
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
    fail("Login failed — check VIN_LOGIN_ID / VIN_PASSWORD secrets!")
print("[\u2713] Login successful!")


# ── SHARED: locate the frame hosting a marker text ───────
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


# ── STEP 2: NAVIGATE TO MASTER > SKU MASTER ──────────────
print("\n[2] Navigating to Master > SKU Master...")
nav_result = page.run_js("""
    function findByText(tag, text) {
        var els = document.querySelectorAll(tag);
        for (var el of els) {
            if ((el.innerText || el.textContent || '').trim() === text) return el;
        }
        return null;
    }
    var direct = findByText('a','SKU Master') || findByText('span','SKU Master') || findByText('li','SKU Master');
    if (direct) { direct.click(); return '[\u2713] Clicked "SKU Master"'; }
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
        if (el) { el.click(); return '[\u2713] Clicked "SKU Master" after opening Master menu'; }
        return '[!] Still not found — manual selector fix needed';
    """)
    print(f"    {nav_result2}")

time.sleep(4)

# ── STEP 2b: FIND THE SKU ENQUIRY FRAME ──────────────────
print("\n[2b] Locating SKU Master frame context...")
ctx, ctx_desc = find_context('SKU Code')
if ctx is None:
    ctx, ctx_desc = find_context('SKU Name')
if ctx is None:
    ctx, ctx_desc = find_context('Advance Search')
if ctx is None:
    fail("Could not find the SKU Master grid in the page or any iframe.")
print(f"    [\u2713] SKU Master content found in: {ctx_desc}")

# (SKU Master has no date filter — go straight to Search.)


# ── HELPER: click a visible element by exact text ────────
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


# ── STEP 4: SEARCH + WAIT FOR DATA ───────────────────────
print("\n[4] Clicking Search...")
ok, info = js_click_visible_text(['Search'], tags=('button', 'a'))
if ok:
    print(f"    [\u2713] {info}")
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
        print(f"    [\u2713] Data present: {rows} row(s) visible.")
        data_ready = True
        break
    time.sleep(1)
if not data_ready:
    fail("No SKU rows detected after Search — aborting (would export empty).")
time.sleep(2)


# ── STEP 5: OPEN EXPORT DIALOG ───────────────────────────
try:
    run_start_epoch = int(ctx.run_js("return Date.now();"))
except Exception:
    run_start_epoch = int(time.time() * 1000)

print("\n[5] Clicking Export (toolbar)...")
ok, info = js_click_visible_text(['Export'], tags=('button', 'a'))
if ok:
    print(f"    [\u2713] {info}")
else:
    fail(f"Could not click toolbar Export ({info}).")
time.sleep(2)


# ── STEP 6: CONFIGURE EXPORT DIALOG ──────────────────────
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
print(f"    [\u2713] Export dialog found in: {dctx_desc}")
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
print("    [\u2713] Now operating inside the export-fields iframe.")

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

def read_field_state():
    return dctx.run_js(_DIALOG_BOXES_JS + """
        if (!structureOK()) return JSON.stringify({total:0, checked:0, bad:'NO_GRID'});
        var fb = fieldBoxes();
        var checked = fb.filter(function(b){ return b.checked; }).length;
        return JSON.stringify({total: fb.length, checked: checked, bad:null});
    """ + _DIALOG_BOXES_JS_END)

def tag_field_boxes():
    raw = dctx.run_js(_DIALOG_BOXES_JS + """
        if (!structureOK()) return JSON.stringify({bad:'NO_GRID'});
        var hb = headerBox();
        if (hb && !hb.id) hb.id = 'AUTO_HDR_BOX';
        var fb = fieldBoxes();
        var list = [];
        fb.forEach(function(b, i){
            if (!b.id) b.id = 'AUTO_FLD_' + i;
            list.push({id: b.id, name: rowNameOf(b), checked: !!b.checked, disabled: !!b.disabled});
        });
        return JSON.stringify({
            bad: null,
            header: hb ? hb.id : null,
            fields: list
        });
    """ + _DIALOG_BOXES_JS_END)
    try:
        return json.loads(raw)
    except Exception:
        return {"bad": "PARSE", "fields": []}

def native_click(box_id):
    try:
        el = dctx.ele(f"#{box_id}", timeout=3)
        if el:
            el.click(by_js=False)
            return True
    except Exception:
        pass
    return False

def select_all_export_fields():
    info = tag_field_boxes()
    if info.get("bad"):
        return f"{info['bad']}"
    fields = info.get("fields", [])
    if not fields:
        return "NO_ROWS"
    unchecked = [f for f in fields if not f["checked"] and not f["disabled"]]
    if info.get("header") and unchecked:
        native_click(info["header"])
        time.sleep(0.8)
    info2 = tag_field_boxes()
    still_off = [f for f in info2.get("fields", []) if not f["checked"] and not f["disabled"]]
    for f in still_off:
        native_click(f["id"])
        time.sleep(0.15)
    final = tag_field_boxes()
    flds = final.get("fields", [])
    checked = sum(1 for f in flds if f["checked"])
    sample = ",".join(f["name"] for f in flds[:8])
    return f"OK|{checked}/{len(flds)} || headerNative || sample={sample}"

fields_ok = False
for attempt in range(1, 4):
    result = select_all_export_fields()
    print(f"    Select fields (try {attempt}) -> {result}")
    if (result or "").startswith(("NO_GRID", "NO_ROWS")):
        time.sleep(1.5)
        continue
    time.sleep(1.5)
    try:
        st = json.loads(read_field_state())
    except Exception:
        st = {"total": 0, "checked": 0, "bad": None}
    if st.get("bad"):
        print(f"    [!] Grid not detected ({st['bad']}) — retrying...")
        time.sleep(1.5)
        continue
    if st["total"] > 0 and st["checked"] == st["total"]:
        fields_ok = True
        break

if not fields_ok:
    fail("Fields not fully selected — export would be rejected.")
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
dlg_clicked = False
try:
    ebtn = export_ctx.ele(f"#{btn_id}", timeout=6)
    if ebtn:
        ebtn.click(by_js=False)
        dlg_clicked = True
        print("    [\u2713] Native click on dialog Export.")
except Exception as e:
    print(f"    Native dialog click failed: {e}")

time.sleep(2)
still_open = outer_dctx.run_js("""
    var els = document.querySelectorAll('div,h1,h2,h3,span');
    for (var el of els){
        if ((el.innerText || '').indexOf('Select Field For Export') !== -1){
            var r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) return true;
        }
    }
    return false;
""")
print(f"    Dialog still open after click? {still_open}")

if not dlg_clicked:
    fail("Could not click dialog Export.")
time.sleep(3)


# ── STEP 7: OPEN PENDING REPORT PAGE ─────────────────────
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
print(f"    [\u2713] Pending grid found in: {pctx_desc}")


# ── STEP 8: WAIT FOR A NEW SkuEnquiryExport, THEN DOWNLOAD ─
print(f"\n[8] Waiting for report (max {MAX_WAIT_SECONDS}s)...")
print("    Checking status every 10 seconds...\n")

def get_latest_report(run_start_epoch):
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
                if (reqTs && reqTs < (RUN_START - 60000)) continue;
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
    """.replace("RUN_START", str(run_start_epoch)))
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
    file_id   = info.get("fileId", "")
    status    = info.get("status", "")
    elapsed   = int(time.time() - start_time)
    print(f"[{elapsed:3d}s] {REPORT_NAME_MATCH} #{report_id}  Status: {status}")

    if status == "SUCCESS":
        print("\n[\u2713] SUCCESS — triggering downloadReport()...")
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
                print(f"    [\u2713] File downloaded: {downloaded_file}")
            else:
                print("    [!] downloadReport fired but no file appeared in time.")
        break

    print("    Waiting 10 seconds...")
    time.sleep(10)
    page.refresh()
    time.sleep(3)

if not downloaded:
    fail("Report did not download within the time limit.")


# ── STEP 9: UPLOAD TO GOOGLE DRIVE ───────────────────────
print("\n[9] Uploading to Google Drive...")
from datetime import datetime
latest_file = downloaded_file or get_latest_downloaded_file(LOCAL_DOWNLOAD_PATH, since_ts=pre_download_ts)
if not latest_file:
    fail(f"Could not find the downloaded file in {LOCAL_DOWNLOAD_PATH}.")

print(f"    [\u2713] Found file: {latest_file}")
drive_filename = "SKU Master " + datetime.now().strftime("%d-%m-%Y")
uploaded_file_id = upload_to_drive(drive_service, latest_file, drive_filename, GDRIVE_FOLDER_ID)

page.quit()

if not uploaded_file_id:
    fail("Drive upload failed. Check that the folder is shared with the service account's client_email.")

print(f"    Link: https://drive.google.com/file/d/{uploaded_file_id}/view")
try:
    os.remove(latest_file)
    print(f"    [\u2713] Local temp file deleted: {latest_file}")
except Exception as e:
    print(f"    [!] Could not delete local temp file: {e}")

print("\n" + "=" * 55)
print("  [\u2713] SUCCESS! SKU Master uploaded to Google Drive as a Sheet.")
print(f"  Drive folder: https://drive.google.com/drive/folders/{GDRIVE_FOLDER_ID}")
print("=" * 55)
