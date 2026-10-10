"""
Shared Vinculum (eRetail) browser helpers for the export scripts.

Why this exists: field selection in the "Select Field For Export" dialog used
coordinate (mouse-event) clicks whose failures were swallowed. When something
covered the page (suspected: one of Vinculum's own top-level Bootstrap modals),
every click missed, the run exported nothing and the day was lost — see
"Errors and their fixes/2026-10-10-order-enquiry-export-fields-3-of-39.md" and
"Errors and their fixes/2026-10-10-sku-master-fragile-clicks.md".

Rules every Vinculum export script follows (via these helpers):
  * close any open top-level Vinculum popup after login, on the report screen,
    before field selection and before a re-try (dismiss_top_modals);
  * select export fields with element.click() run INSIDE the export-fields
    iframe, verify both the checkbox count and jqGrid's own selection, use
    native clicks only as a logged fallback, and never export a partial
    selection (select_all_export_fields);
  * fire the dialog's Export button natively, fall back to a JS click, and
    fail if the dialog is still open (request_dialog_export);
  * a fresh browser per attempt (launch_browser) and a screenshot on failure
    (save_debug_screenshot).

DrissionPage is imported lazily so the pure helpers (all_fields_selected) can
be unit-tested without a browser.
"""

import json
import os
import time


# ── Popups ───────────────────────────────────────────────────
# Vinculum's top-level page carries hidden Bootstrap modals (z-index 1050 + a
# full-page backdrop) that it opens on its own: #screenNotificationDialog
# ("Remind me after 1 Hour ... / don't show this notification again") and
# #feedbackFormDialog ("Take the Survey / Not Now"), among others. While one is
# open, every coordinate click lands on its backdrop, but JS clicks still work.
# Only the TOP document is touched (the export dialog lives inside an iframe).
DISMISS_TOP_MODALS_JS = """
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
        res = json.loads(page.run_js(DISMISS_TOP_MODALS_JS) or "{}")
    except Exception as e:
        print(f"    [!] popup check failed ({where}): {e!r}")
        return []
    if res.get("closed") or res.get("backdrops"):
        print(f"    [!] Closed Vinculum popup(s) {where}: {res.get('closed')} "
              f"(backdrops removed: {res.get('backdrops')})")
    return res.get("closed") or []


# ── Browser / debug ──────────────────────────────────────────
def launch_browser(download_path):
    """Headless Chrome on a fresh port + profile, so a retry never reuses a
    dead session."""
    from DrissionPage import ChromiumPage, ChromiumOptions
    co = ChromiumOptions()
    co.headless(True)
    co.auto_port()
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-gpu")
    co.set_argument("--window-size=1920,1080")
    page = ChromiumPage(addr_or_opts=co)
    page.set.download_path(download_path)
    return page


def save_debug_screenshot(page, debug_dir, name):
    try:
        os.makedirs(debug_dir, exist_ok=True)
        page.get_screenshot(path=debug_dir, name=name, full_page=False)
        print(f"    [debug] screenshot saved: {debug_dir}/{name}")
    except Exception as e:
        print(f"    [debug] screenshot failed: {e!r}")


# ── Export-fields dialog (runs inside dynamicFieldDialogIFrame) ──
DIALOG_BOXES_JS = """
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

    function structureOK(){
        var hasSelect = document.querySelectorAll('select').length > 0;
        var hasTable  = document.querySelectorAll('table').length > 0;
        var boxes     = fieldBoxes().length;
        return hasSelect && hasTable && boxes > 0;
    }
"""
DIALOG_BOXES_JS_END = "})();"

_READ_FIELD_STATE_JS = DIALOG_BOXES_JS + """
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
""" + DIALOG_BOXES_JS_END

_JS_CLICK_UNCHECKED_JS = DIALOG_BOXES_JS + """
    if (!structureOK()) return JSON.stringify({bad:'NO_GRID', clicked:0});
    var n = 0;
    fieldBoxes().forEach(function(b){
        if (!b.checked && !b.disabled) { b.click(); n++; }
    });
    return JSON.stringify({bad:null, clicked:n});
""" + DIALOG_BOXES_JS_END

_TAG_UNCHECKED_JS = DIALOG_BOXES_JS + """
    var out = [];
    fieldBoxes().forEach(function(b, i){
        if (!b.checked && !b.disabled) { if (!b.id) b.id = 'AUTO_FLD_' + i; out.push(b.id); }
    });
    return JSON.stringify(out);
""" + DIALOG_BOXES_JS_END


def parse_field_state(raw):
    """JSON from _READ_FIELD_STATE_JS -> dict (never raises)."""
    try:
        st = json.loads(raw)
        if not isinstance(st, dict):
            raise ValueError
        return st
    except Exception:
        return {"total": 0, "checked": 0, "bad": "PARSE", "unchecked": []}


def all_fields_selected(st):
    """True only if every field box is checked AND (when the grid is a jqGrid
    multiselect) jqGrid's own selection list — what the export reads — agrees."""
    if st.get("bad") or not st.get("total"):
        return False
    if st.get("checked") != st["total"]:
        return False
    if st.get("jqSelected") is not None and st["jqSelected"] != st["total"]:
        return False
    return True


def read_field_state(dctx):
    try:
        return parse_field_state(dctx.run_js(_READ_FIELD_STATE_JS))
    except Exception as e:
        return {"total": 0, "checked": 0, "bad": f"JS_ERROR {e!r}", "unchecked": []}


def js_click_unchecked(dctx):
    """element.click() on every unchecked, enabled field box, run in the iframe:
    fires jqGrid's own row-checkbox handler regardless of what is drawn on top
    of the page or of rows scrolled out of view. The header box is a TOGGLE
    (clicking it when all are selected clears them) and is never clicked."""
    try:
        return json.loads(dctx.run_js(_JS_CLICK_UNCHECKED_JS))
    except Exception:
        return {"bad": "PARSE", "clicked": 0}


def native_click_unchecked(dctx):
    """Fallback only: mouse-event click on each still-unchecked row box.
    Returns how many clicks DrissionPage reports as dispatched."""
    try:
        ids = json.loads(dctx.run_js(_TAG_UNCHECKED_JS) or "[]")
    except Exception:
        ids = []
    done = 0
    for box_id in ids:
        try:
            el = dctx.ele(f"#{box_id}", timeout=3)
            if el and el.click(by_js=False):
                done += 1
        except Exception as e:
            print(f"      native click #{box_id} failed: {e!r}")
        time.sleep(0.15)
    return done


def select_all_export_fields(page, dctx, tries=3):
    """Selects every export field in the dialog's iframe (dctx) and verifies it.
    Returns (ok, state). Callers must refuse to export when ok is False."""
    st = read_field_state(dctx)
    print(f"    Fields before: {st.get('checked')}/{st.get('total')} checked"
          f" (jqGrid selected {st.get('jqSelected')}/{st.get('jqRows')})")
    for attempt in range(1, tries + 1):
        if all_fields_selected(st):
            return True, st
        if st.get("bad"):
            print(f"    [!] Field grid not ready ({st['bad']}) — waiting...")
            time.sleep(1.5)
            st = read_field_state(dctx)
            continue
        res = js_click_unchecked(dctx)
        time.sleep(1)
        st = read_field_state(dctx)
        print(f"    Select fields (try {attempt}) JS-click {res.get('clicked')} boxes -> "
              f"{st.get('checked')}/{st.get('total')} checked, jqGrid selected "
              f"{st.get('jqSelected')}/{st.get('jqRows')}")
        if all_fields_selected(st):
            return True, st
        n = native_click_unchecked(dctx)
        time.sleep(1)
        st = read_field_state(dctx)
        print(f"    Select fields (try {attempt}) native-click fallback {n} boxes -> "
              f"{st.get('checked')}/{st.get('total')} checked")
        if all_fields_selected(st):
            return True, st
        dismiss_top_modals(page, "before re-trying field selection")
    return all_fields_selected(st), st


# ── Dialog Export button ─────────────────────────────────────
_EXPORT_FINDER_JS = """
    return (function(){
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


def request_dialog_export(dctx, outer_dctx):
    """Clicks the dialog's Export button (inner iframe first, then the outer
    modal). Native click first; if the dialog is still open, the button's own
    click handler is fired by JS. Returns (ok, message); ok=False means the
    export was NOT requested."""
    export_ctx, tag_result = None, "NO_DIALOG_EXPORT"
    for label, cxt in (("inner iframe", dctx), ("outer modal", outer_dctx)):
        try:
            res = cxt.run_js(_EXPORT_FINDER_JS)
        except Exception as e:
            res = f"ERR {e!r}"
        print(f"    export button search ({label}) -> {res}")
        if (res or "").startswith("FOUND"):
            export_ctx, tag_result = cxt, res
            break
    if export_ctx is None:
        return False, "Could not isolate the dialog Export button."
    btn_id = tag_result.split("|")[1]

    def dialog_open():
        try:
            return bool(outer_dctx.run_js(_DIALOG_OPEN_JS))
        except Exception:
            return False

    clicked = False
    try:
        ebtn = export_ctx.ele(f"#{btn_id}", timeout=6)
        if ebtn and ebtn.click(by_js=False):
            clicked = True
            print("    [✓] Native click on dialog Export.")
    except Exception as e:
        print(f"    Native dialog click failed: {e}")

    time.sleep(2)
    still_open = dialog_open()
    print(f"    Dialog still open after click? {still_open}")
    if still_open:
        # The native click did not land (same failure class as the field
        # checkboxes) — fire the button's own click handler instead.
        try:
            res = export_ctx.run_js(
                f"var b = document.getElementById({json.dumps(btn_id)});"
                " if (!b) return 'NO_BUTTON'; b.click(); return 'JS_CLICKED';")
        except Exception as e:
            res = f"ERR {e!r}"
        print(f"    Dialog Export JS-click fallback -> {res}")
        clicked = clicked or res == "JS_CLICKED"
        time.sleep(3)
        still_open = dialog_open()
        print(f"    Dialog still open after JS click? {still_open}")
        if still_open:
            return False, "Export dialog did not close after clicking Export — export not requested."
    if not clicked:
        return False, "Could not click dialog Export."
    return True, "requested"
