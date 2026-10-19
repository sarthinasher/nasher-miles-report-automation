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
import re
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


# ── Login (2026-10-13) ───────────────────────────────────────
# Every report job failed from 12-Oct with only "Login failed — check VIN_LOGIN_ID /
# VIN_PASSWORD secrets." and a screenshot of the login page: the alert Vinculum showed
# was accepted and thrown away, so the reason (wrong / expired password, locked user)
# was never logged, and each job retried the login several times (risking a lock-out).
# vinculum_login() logs the alert / on-page message and raises LoginError, which the
# callers treat as NOT retryable for the whole run. Values are passed to JS with
# json.dumps, so a quote or backslash in the password no longer breaks the login JS.
LOGIN_MESSAGE_JS = """
    return (function(){
        var sel = ['.error', '.errorMessage', '.errormsg', '.alert', '.alert-danger',
                   '.text-danger', '#errorMsg', '#msg', '.loginError', '.help-block',
                   'font[color=red]', 'span[style*="red"]', 'div[style*="red"]',
                   '#notification', '.actionError', '.errorMessage li'];
        var out = [];
        sel.forEach(function(s){
            document.querySelectorAll(s).forEach(function(e){
                var t = (e.innerText || '').replace(/\\s+/g, ' ').trim();
                var r = e.getBoundingClientRect();
                if (t && r.width > 0 && r.height > 0 && out.indexOf(t) < 0) out.push(t.slice(0, 200));
            });
        });
        return out.join(' | ');
    })();
"""


class LoginError(Exception):
    """Vinculum refused the login. Never retried within a run."""


def login_failure_message(alert_text, page_text, login_id):
    """One log line saying what Vinculum said and what to do about it."""
    alert_text = (alert_text or "").strip()
    page_text = (page_text or "").strip()
    said = "; ".join(p for p in (f'alert "{alert_text}"' if alert_text else "",
                                 f'page "{page_text}"' if page_text else "") if p)
    said = said or "no message shown, still on the login page"
    low = f"{alert_text} {page_text}".lower()
    if any(k in low for k in ("expire", "change your password", "change password", "reset")):
        hint = ("the password has EXPIRED - log in once by hand as this user, set a new "
                "password, then update the VIN_PASSWORD secret")
    elif any(k in low for k in ("lock", "disabled", "inactive", "deactivat", "blocked", "suspend")):
        hint = ("the user is LOCKED / DISABLED in Vinculum - ask the Vinculum admin to "
                "unlock it, or put another user in VIN_LOGIN_ID / VIN_PASSWORD")
    elif any(k in low for k in ("invalid", "incorrect", "wrong", "not match", "not valid")):
        hint = "Vinculum rejected the user / password - update VIN_LOGIN_ID / VIN_PASSWORD"
    elif any(k in low for k in ("already logged", "another session", "concurrent")):
        hint = "the user has another active session"
    else:
        hint = ("check VIN_LOGIN_ID / VIN_PASSWORD - log in by hand as this user to see "
                "what Vinculum asks for")
    return (f"Vinculum login failed for user '{login_id}': {said} -> {hint}. "
            f"Not retried this run (repeated failed logins can lock the user).")


def vinculum_login(page, base, login_id, password, org_id=""):
    """Logs in on the eRetail login page. Raises LoginError with Vinculum's reason."""
    page.get(f"{base}/eRetailLogin.action?popup=true")
    time.sleep(4)
    for selector in ("[name=userOrgId]", "[name=userName]", "[id=password]"):
        page.run_js(f"var e = document.querySelector({json.dumps(selector)}); "
                    f"if (e) e.removeAttribute('readonly');")
    time.sleep(1)
    values = [("[name=userName]", login_id), ("[id=password]", password)]
    if org_id:
        values.insert(0, ("[name=userOrgId]", org_id))
    for selector, value in values:
        page.run_js(f"document.querySelector({json.dumps(selector)}).value = {json.dumps(value)};")
    time.sleep(1)
    page.run_js("document.querySelector('[name=Login][type=submit]').click()")
    time.sleep(3)

    alerts = []
    for _ in range(2):            # e.g. "already logged in - continue?" then a notice
        try:
            try:
                text = page.handle_alert(accept=True, timeout=3)
            except TypeError:                     # older DrissionPage: no timeout kw
                text = page.handle_alert(accept=True)
        except Exception:
            text = None
        if not text:
            break
        alerts.append(str(text))
        time.sleep(2)
    try:
        ok = page.ele("xpath://button[contains(text(),'OK')]", timeout=3)
        if ok:
            ok.click()
            time.sleep(2)
    except Exception:
        pass
    time.sleep(4)

    if "login" in (page.url or "").lower():
        try:
            page_text = page.run_js(LOGIN_MESSAGE_JS) or ""
        except Exception:
            page_text = ""
        url = re.sub(r";jsessionid=[^?#]*", "", page.url or "")
        raise LoginError(login_failure_message(" | ".join(alerts), page_text, login_id)
                         + f" [page after submit: {url}]")
    if alerts:
        print(f"    Vinculum said at login (accepted): {' | '.join(alerts)}")
    print("[✓] Login successful!")


# ── Report date (2026-10-13) ─────────────────────────────────
# Same daterangepicker call that the Order Enquiry export has used since 10-Oct, so a
# report can be exported for ANY past day (catch-up / back-fill), not only "Yesterday".
def set_date_range_js(day_str):
    d = json.dumps(day_str)
    return f"""
        try {{
            if (!window.jQuery) return "NO_JQUERY";
            if (!window.moment) return "NO_MOMENT";
            var $ = window.jQuery, cands = [];
            $('input').each(function() {{ if ($(this).data('daterangepicker')) cands.push(this); }});
            if (!cands.length) return "NO_DATE_INPUT";
            var vis = cands.filter(function(e) {{ return e.offsetWidth > 0; }});
            var target = vis.filter(function(e) {{ return (e.value || '').indexOf('/') !== -1; }})[0]
                         || vis[0] || cands[0];
            var drp = $(target).data('daterangepicker');
            drp.setStartDate(moment({d}, "DD/MM/YYYY").startOf('day'));
            drp.setEndDate(moment({d}, "DD/MM/YYYY").endOf('day'));
            $(target).trigger('apply.daterangepicker', drp);
            return "OK|" + drp.startDate.format('DD/MM/YYYY') + "|" +
                   drp.endDate.format('DD/MM/YYYY') + "|" + (target.value || '');
        }} catch (e) {{ return "ERR|" + e.toString(); }}
    """


def date_range_ok(result, day_str):
    """True if set_date_range_js reported start == end == day_str."""
    parts = str(result or "").split("|")
    return len(parts) >= 3 and parts[0] == "OK" and parts[1] == day_str and parts[2] == day_str


def set_report_date(page, day_str):
    """Sets the report's date picker to day_str (DD/MM/YYYY). Returns (ok, detail)."""
    try:
        res = page.run_js(set_date_range_js(day_str))
    except Exception as e:
        res = f"ERR|{e!r}"
    return date_range_ok(res, day_str), res


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


# ── Locations / warehouses (2026-10-19) ──────────────────────
# Every Vinculum export only covers the session's CURRENT location: the report
# screens' "Site Location" select (Pick Pack #siteLocationCode, Sales #siteLoc) lists
# that one location only. The old "Diwakar" user's exports held every warehouse; the
# user that replaced it on 2026-10-19 lands on NM4 and the files shrank ~10x (see
# "Errors and their fixes/2026-10-19-new-vinculum-login-exports-only-nm4.md").
# The header's "Switch Location" dialog (selCompanyLocationBS?operation=switch:
# Company / Location / ... / Set As Default | Remove Default | Do Nothing) lists every
# location the user is mapped to and has no "All" entry, so the report scripts export
# once per location and merge the files (common/report_merge.py). The switch always
# posts setDefault=remainSame ("Do Nothing"): the user's default is never changed.
# VIN_LOCATIONS (env) picks the locations: blank / "all" = every location offered;
# "A03,NM4" = exactly those; "all,-S01,-S02" = all but those.
SWITCH_LOCATION_PAGE = "selCompanyLocationBS?operation=switch"

READ_LOCATIONS_JS = """
    return (function(){
        var sel = document.getElementById('locationDetail');
        if (!sel) return JSON.stringify({bad: 'NO_LOCATION_SELECT'});
        var cur = document.getElementById('location');
        var comp = document.getElementById('companyDetail');
        var opts = [];
        for (var i = 0; i < sel.options.length; i++) {
            var o = sel.options[i];
            if (o.value && o.value !== '-1') opts.push([o.value, (o.text || '').trim()]);
        }
        return JSON.stringify({bad: null, current: cur ? cur.value : '',
                               selected: sel.value, company: comp ? comp.value : '',
                               options: opts});
    })();
"""


def switch_location_js(loc):
    """Selects loc on the Switch Location page, keeps "Do Nothing" for the default
    location and calls the page's own doCompanyChange() a moment later (so run_js
    returns before the page reloads)."""
    lj = json.dumps(loc)
    return f"""
        return (function(){{
            var sel = document.getElementById('locationDetail');
            if (!sel) return 'NO_LOCATION_SELECT';
            var has = false;
            for (var i = 0; i < sel.options.length; i++) if (sel.options[i].value === {lj}) has = true;
            if (!has) return 'NO_OPTION';
            if (window.jQuery) jQuery(sel).val({lj}).trigger('change');
            else {{ sel.value = {lj}; if (sel.onchange) sel.onchange(); }}
            var radios = document.querySelectorAll('input[name=setDefault]');
            for (var j = 0; j < radios.length; j++) radios[j].checked = (radios[j].value === 'remainSame');
            if (radios.length && !document.querySelector('input[name=setDefault][value=remainSame]:checked'))
                return 'NO_DO_NOTHING_OPTION';
            if (typeof doCompanyChange !== 'function') return 'NO_SUBMIT_FUNCTION';
            if (sel.value !== {lj}) return 'NOT_SELECTED|' + sel.value;
            setTimeout(function(){{ doCompanyChange(); }}, 100);
            return 'SUBMITTING';
        }})();
    """


class LocationError(Exception):
    """A location could not be listed / switched to. Retryable (fresh browser)."""
    quiet = True


def parse_locations(raw):
    """JSON from READ_LOCATIONS_JS -> dict (never raises)."""
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            d.setdefault("options", [])
            return d
    except Exception:
        pass
    return {"bad": "PARSE", "options": [], "current": ""}


def resolve_locations(available, spec):
    """Which location codes to export, in the order Vinculum lists them (or the
    order given). available = [code, ...]; spec = the VIN_LOCATIONS value:
      "" / "all" / "*"   -> every available location
      "A03, nm4"         -> exactly those (case-insensitive)
      "all,-S01,-S02"    -> all but S01 / S02
    Raises LocationError for a code the user cannot switch to (never silently
    skipped) or when nothing is left."""
    avail = [a.strip().upper() for a in available if a and a.strip()]
    tokens = [t.strip().upper() for t in (spec or "").replace(";", ",").split(",") if t.strip()]
    include = [t for t in tokens if not t.startswith("-")]
    exclude = {t[1:].strip() for t in tokens if t.startswith("-")}
    if not include or any(t in ("ALL", "*") for t in include):
        chosen = list(avail)
    else:
        unknown = [t for t in include if t not in avail]
        if unknown:
            raise LocationError(f"VIN_LOCATIONS asks for {', '.join(unknown)}, but this Vinculum "
                                f"user can only switch to: {', '.join(avail) or '(none)'}")
        chosen = []
        for t in include:
            if t not in chosen:
                chosen.append(t)
    unknown_ex = sorted(t for t in exclude if t not in avail)
    if unknown_ex:
        print(f"    [!] VIN_LOCATIONS excludes {', '.join(unknown_ex)}, which this user "
              f"cannot switch to anyway.")
    chosen = [c for c in chosen if c not in exclude]
    if not chosen:
        raise LocationError(f"VIN_LOCATIONS={spec!r} leaves no location to export "
                            f"(available: {', '.join(avail) or '(none)'})")
    return chosen


def read_locations(page, base):
    """Opens the Switch Location page; returns {current, options: [[code, name]], ...}."""
    page.get(f"{base}/{SWITCH_LOCATION_PAGE}")
    time.sleep(4)
    try:
        info = parse_locations(page.run_js(READ_LOCATIONS_JS))
    except Exception as e:
        info = {"bad": f"JS_ERROR {e!r}", "options": [], "current": ""}
    if info.get("bad"):
        raise LocationError(f"Could not read the Switch Location page ({info['bad']}).")
    return info


def plan_locations(page, base, spec=None):
    """Logs every location the user can switch to; returns (codes_to_export,
    session_location). Call right after vinculum_login."""
    spec = os.environ.get("VIN_LOCATIONS", "") if spec is None else spec
    info = read_locations(page, base)
    names = {c.strip().upper(): n for c, n in info["options"]}
    current = (info.get("current") or info.get("selected") or "").strip().upper()
    print(f"    Vinculum locations this user can switch to ({len(names)}): "
          + ", ".join(f"{c} ({n})" for c, n in names.items()))
    print(f"    Session location after login: {current or '?'}")
    chosen = resolve_locations(list(names), spec)
    print(f"    VIN_LOCATIONS={spec!r} -> exporting {len(chosen)} location(s): {', '.join(chosen)}")
    return chosen, current


def switch_location(page, base, loc, tries=2):
    """Makes loc the session location (setDefault = "Do Nothing") and verifies it
    on the Switch Location page. Raises LocationError if it did not take."""
    loc = loc.strip().upper()
    last = ""
    for attempt in range(1, tries + 1):
        page.get(f"{base}/{SWITCH_LOCATION_PAGE}")
        time.sleep(4)
        try:
            res = page.run_js(switch_location_js(loc))
        except Exception as e:
            res = f"ERR {e!r}"
        if res != "SUBMITTING":
            last = f"switch page said {res}"
            print(f"    [!] Switch to {loc} (try {attempt}): {last}")
            continue
        time.sleep(3)
        said = read_alert(page, timeout=2)
        if said:
            print(f"    [!] Vinculum said while switching to {loc}: {said}")
        time.sleep(4)
        try:
            cur = (read_locations(page, base).get("current") or "").strip().upper()
        except LocationError as e:
            cur = ""
            print(f"    [!] {e}")
        if cur == loc:
            print(f"    [✓] Vinculum location is now {loc}")
            return
        last = f"session location is still {cur or '?'}"
        print(f"    [!] Switch to {loc} (try {attempt}) did not take: {last}")
    raise LocationError(f"Could not switch the Vinculum location to {loc} ({last}).")


SITE_LOCATION_JS = """
    return (function(){
        var s = document.querySelector('#siteLocationCode, #siteLoc, select[name=loc], select[name="reportDTO.siteLoc"]');
        if (!s) return '';
        var opts = [];
        for (var i = 0; i < s.options.length; i++) opts.push(s.options[i].value);
        return s.value + '|' + opts.join(',');
    })();
"""


def check_site_location(ctx, loc):
    """On a report screen with a "Site Location" select: True if it is set to loc,
    False if it shows another location, None if the screen has no such select."""
    try:
        raw = ctx.run_js(SITE_LOCATION_JS) or ""
    except Exception:
        raw = ""
    if not raw:
        return None
    value, _, opts = raw.partition("|")
    print(f"    Site Location on screen: {value} (options: {opts})")
    return value.strip().upper() == loc.strip().upper()


# Strict on purpose: a message wrongly taken as "no data" would silently drop that
# location from the merged file; anything else just waits for the report (and fails
# loudly if none comes).
NO_DATA_RX = re.compile(r"\bno\s+(data|records?|results?)\b", re.I)


def is_no_data_message(text):
    """True for Vinculum's 'No Record Found' / 'No data found' style messages."""
    return bool(text and NO_DATA_RX.search(str(text)))


def read_alert(page, timeout=2):
    """Accepts a JS alert if one is open and returns its text, else ''."""
    try:
        try:
            text = page.handle_alert(accept=True, timeout=timeout)
        except TypeError:                         # older DrissionPage: no timeout kw
            text = page.handle_alert(accept=True)
    except Exception:
        text = None
    return str(text) if text else ""


def read_page_message(page, timeout=2):
    """Alert text (accepted) or the visible on-page message after a click, or ''."""
    text = read_alert(page, timeout)
    if text:
        return text
    try:
        return page.run_js(LOGIN_MESSAGE_JS) or ""
    except Exception:
        return ""


def export_all_locations(page, base, export_one, spec=None):
    """Runs export_one(loc) -> local file (or None = no data at loc) for every
    location VIN_LOCATIONS selects, switching the session location in between.
    Starts with the session's own location (one switch less) and switches back
    to it at the end. Returns [(loc, file_or_None)] in export order. Any failure
    (switch or export) raises: a partial merge is never uploaded."""
    locs, original = plan_locations(page, base, spec)
    order = ([original] if original in locs else []) + [x for x in locs if x != original]
    parts, current = [], original
    try:
        for i, loc in enumerate(order, 1):
            print(f"\n  ── Location {loc} ({i}/{len(order)}) ──")
            if loc != current:
                switch_location(page, base, loc)
                current = loc
            parts.append((loc, export_one(loc)))
    finally:
        if original and current != original:
            try:
                switch_location(page, base, original)
            except Exception as e:
                print(f"    [!] Could not switch back to {original}: {e}")
    return parts
