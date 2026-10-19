"""
Vinculum location / warehouse probe (diagnostic, READ-ONLY) — 2026-10-19
=========================================================================
Since VIN_LOGIN_ID was switched from the disabled "Diwakar" user to a new user on
2026-10-19, the exports contain ONE warehouse only (Pick Pack: every row Source WH =
NM4; before: A03, A06, ...). No script ever selects a location: they all use the
session's default. This probe finds out whether the UI offers a location switch.

Part A (Google Drive, read-only): counts rows per Source WH / location column in a
few old (old login) and new (new login) report files.

Part B (Vinculum, read-only): logs in with common.vinculum_ui.vinculum_login, then
on the login page, the home page, Pick Pack, Order Enquiry and Sales it logs every
<select> (id / name / label / every option value+text, which one is selected), every
input / link / button / label whose id, name or text looks like location / warehouse
/ facility / WH, window globals and cookie NAMES with such names, and the items of
every header dropdown menu (opening a dropdown toggle is the only click; links are
never followed, nothing is saved or submitted). Screenshots + redacted HTML go to
PROBE_DIR (uploaded as the workflow artifact).

Never logs the password; the login id is replaced by *** in every saved file.
"""

import csv
import io
import json
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.vinculum_ui import (  # noqa: E402
    dismiss_top_modals, launch_browser, save_debug_screenshot, vinculum_login,
)

LOGIN_ID = os.environ.get("VIN_LOGIN_ID") or ""
PASSWORD = os.environ.get("VIN_PASSWORD") or ""
ORG_ID = os.environ.get("VIN_ORG_ID", "")
BASE = "https://nashermiles.vineretail.com/eRetailWeb"
PROBE_DIR = os.environ.get("PROBE_DIR", "/tmp/vinculum_probe")

# (label, Drive file id) — Pick Pack and Order Enquiry files from before / after the
# login change. Override with PROBE_DRIVE_FILES='[["label","id"], ...]'.
DEFAULT_DRIVE_FILES = [
    ("PickPackReport_08.10.2026 (old login)", "1E1F_6uEVPS5BdIMj_R90VWInsM6hESdi-mrQEJp3L_w"),
    ("PickPackReport_09.10.2026 (old login)", "1FGnxzi2ddR9SgV6TEJOwQ7fZQJ4mswZz-sJ1AIVgP70"),
    ("PickPackReport_10.10.2026 (old login)", "16wkLTA4JYuyzxAoQOpPnUA4v-iNYUyEZPNpzAm9orgU"),
    ("PickPackReport_15.10.2026 (new login)", "1CP-YGGXWC15r0_W5YADQME3cfudOy6eHLY0hZZQ8zcA"),
    ("PickPackReport_17.10.2026 (new login)", "1Bv5B5XXlb9nC_OQ9D6jcsXrznnE1UB5hrUhqHsNWv54"),
    ("OrderEnquiry_09-10-2026 (old login)", "1hSvEo75HYgfFu10K_iQDTkzxorGEMLGgweAlpM0tydU"),
    ("OrderEnquiry_15-10-2026 (new login)", "1qLYo8CK5_fKS3Xsoa08mR0WA3tvXoHJ_lvZwrdwYoBk"),
]
LOC_COLUMN_RX = re.compile(r"(source\s*wh|\bwh\b|warehouse|location|facility|\bloc\b|fulfil)", re.I)


def redact(text):
    text = str(text if text is not None else "")
    for secret in (PASSWORD, LOGIN_ID):
        if secret and len(secret) >= 3:
            text = re.sub(re.escape(secret), "***", text, flags=re.I)
    return text


def out(line=""):
    print(redact(line), flush=True)


def save_text(name, content):
    os.makedirs(PROBE_DIR, exist_ok=True)
    with open(os.path.join(PROBE_DIR, name), "w", encoding="utf-8") as f:
        f.write(redact(content))


# ── Part A: Drive counts ─────────────────────────────────────
def location_counts(rows):
    """rows = list of lists (header first). Returns (n_rows, {column: Counter})."""
    if not rows:
        return 0, {}
    header, data = rows[0], [r for r in rows[1:] if any((c or "").strip() for c in r)]
    counts = {}
    for i, col in enumerate(header):
        if LOC_COLUMN_RX.search(col or ""):
            counts[col] = Counter((r[i] if i < len(r) else "").strip() or "(blank)" for r in data)
    return len(data), counts


def drive_part():
    out("\n" + "=" * 70)
    out("PART A - Google Drive: rows per warehouse / location column (read-only)")
    out("=" * 70)
    if not os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON"):
        out("  GDRIVE_SERVICE_ACCOUNT_JSON not set - skipped.")
        return
    try:
        from common.drive_utils import get_drive_service
        svc = get_drive_service()
    except Exception as e:
        out(f"  Drive auth failed: {e!r} - skipped.")
        return
    files = DEFAULT_DRIVE_FILES
    if os.environ.get("PROBE_DRIVE_FILES"):
        files = [tuple(x) for x in json.loads(os.environ["PROBE_DRIVE_FILES"])]
    for label, file_id in files:
        try:
            raw = svc.files().export_media(fileId=file_id, mimeType="text/csv").execute()
            rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig", errors="replace"))))
        except Exception as e:
            out(f"\n  {label}: could not read ({e!r})")
            continue
        n, counts = location_counts(rows)
        out(f"\n  {label}: {n} data rows; columns: {len(rows[0]) if rows else 0}")
        if not counts:
            out(f"    no warehouse/location-like column. Header: {rows[0] if rows else []}")
        for col, c in counts.items():
            out(f"    [{col}] " + ", ".join(f"{k}={v}" for k, v in c.most_common(40)))
        if rows and "Order No" in rows[0]:
            i = rows[0].index("Order No")
            out(f"    distinct Order No: {len({r[i] for r in rows[1:] if i < len(r) and r[i]})}")


# ── Part B: Vinculum UI ──────────────────────────────────────
DUMP_JS = r"""
return (function(){
  var RX = /(loc|wh(s)?\b|^wh|warehouse|facility|branch|site|node|fc\b)/i;
  var TXT = /(location|warehouse|facility|\bwh\b|\bwhs\b|branch|change loc|switch|node)/i;
  function vis(e){ try { var r=e.getBoundingClientRect(), st=getComputedStyle(e);
      return r.width>0 && r.height>0 && st.visibility!=='hidden' && st.display!=='none'; } catch(x){ return false; } }
  function clean(t){ return (t||'').replace(/\s+/g,' ').trim(); }
  function labelOf(e){
    var t='';
    try {
      if (e.id) { var l=document.querySelector('label[for="'+e.id.replace(/"/g,'')+'"]'); if (l) t=clean(l.innerText); }
      if (!t) { var p=e.closest('label'); if (p) t=clean(p.innerText); }
      if (!t) { var c=e.closest('.form-group,td,li,div'); if (c) { var l2=c.querySelector('label'); if (l2) t=clean(l2.innerText); } }
      if (!t) { var prev=e.parentElement && e.parentElement.previousElementSibling; if (prev) t=clean(prev.innerText); }
    } catch(x){}
    return t.slice(0,80);
  }
  var selects=[].slice.call(document.querySelectorAll('select')).map(function(s){
    return {id:s.id, name:s.name, cls:clean(s.className).slice(0,60), visible:vis(s), multiple:s.multiple,
            label:labelOf(s), onchange:(s.getAttribute('onchange')||'').slice(0,200),
            selected:[].slice.call(s.selectedOptions||[]).map(function(o){return o.value+' = '+clean(o.text);}),
            n:s.options.length,
            options:[].slice.call(s.options).slice(0,400).map(function(o){return [o.value, clean(o.text), o.selected];})};
  });
  var inputs=[].slice.call(document.querySelectorAll('input,textarea')).filter(function(i){
    return i.type!=='password' && RX.test((i.id||'')+' '+(i.name||'')+' '+(i.placeholder||'')+' '+labelOf(i));
  }).map(function(i){ return {id:i.id, name:i.name, type:i.type, value:(i.value||'').slice(0,120),
      placeholder:i.placeholder||'', visible:vis(i), label:labelOf(i)}; });
  var texts=[]; var seen={};
  [].slice.call(document.querySelectorAll('a,button,span,li,label,td,th,h1,h2,h3,h4,h5,h6,b,strong,p,div')).forEach(function(e){
    if (texts.length>=250) return;
    var own = clean([].slice.call(e.childNodes).filter(function(n){return n.nodeType===3;}).map(function(n){return n.textContent;}).join(' '));
    var t = own || (e.children.length===0 ? clean(e.innerText) : '');
    var attrs=(e.id||'')+' '+(e.getAttribute('onclick')||'')+' '+(e.getAttribute('href')||'');
    if (!(t && t.length<=80 && TXT.test(t)) && !RX.test(attrs.replace(/javascript/ig,''))) return;
    var key=e.tagName+'|'+t+'|'+attrs; if (seen[key]) return; seen[key]=1;
    texts.push({tag:e.tagName, id:e.id, cls:clean(e.className&&e.className.baseVal!==undefined?e.className.baseVal:e.className).slice(0,60),
                text:t.slice(0,80), href:(e.getAttribute('href')||'').slice(0,200),
                onclick:(e.getAttribute('onclick')||'').slice(0,200), visible:vis(e)});
  });
  var globals=[];
  try { Object.keys(window).forEach(function(k){
    if (!RX.test(k)) return; var v; try { v=window[k]; } catch(x){ return; }
    if (['string','number','boolean'].indexOf(typeof v)>=0) globals.push(k+' = '+String(v).slice(0,120));
    else if (v && typeof v==='object' && !(v instanceof Node) && !(v===window)) { try { globals.push(k+' = '+JSON.stringify(v).slice(0,300)); } catch(x){} }
  }); } catch(x){}
  var cookies=(document.cookie||'').split(';').map(function(c){ var p=c.split('='); var k=clean(p[0]);
      return RX.test(k) ? k+'='+clean(p.slice(1).join('=')).slice(0,80) : k; }).filter(Boolean);
  var hdr=document.querySelector('header,.navbar,#header,.main-header,.top-header,.topbar,.top-bar,.page-header');
  var dd=[].slice.call(document.querySelectorAll('[data-toggle=dropdown],.dropdown-toggle')).map(function(e,i){
    if (!e.id) e.id='PROBE_DD_'+i;
    return {id:e.id, tag:e.tagName, text:clean(e.innerText).slice(0,60), href:(e.getAttribute('href')||''), visible:vis(e)}; });
  return JSON.stringify({url:location.href, title:document.title, selects:selects, inputs:inputs, texts:texts,
    globals:globals, cookies:cookies, header:hdr?clean(hdr.innerText).slice(0,600):'', dropdowns:dd,
    iframes:[].slice.call(document.querySelectorAll('iframe')).map(function(f){return (f.id||'')+' '+(f.getAttribute('src')||'').slice(0,150);})});
})();
"""

VISIBLE_MENU_JS = r"""
return (function(){
  function vis(e){ var r=e.getBoundingClientRect(), st=getComputedStyle(e);
      return r.width>0 && r.height>0 && st.visibility!=='hidden' && st.display!=='none'; }
  function clean(t){ return (t||'').replace(/\s+/g,' ').trim(); }
  var items=[];
  [].slice.call(document.querySelectorAll('.dropdown-menu, .dropdown.open ul, .open > ul, [role=menu]')).forEach(function(m){
    if (!vis(m)) return;
    [].slice.call(m.querySelectorAll('a,li,button,select,input')).forEach(function(a){
      if (!vis(a)) return;
      var t = a.tagName==='SELECT' ? ('SELECT#'+a.id+' '+[].slice.call(a.options).map(function(o){return o.value+'='+clean(o.text)+(o.selected?'*':'');}).join(', '))
                                   : clean(a.innerText).slice(0,80);
      items.push(a.tagName+' | '+t+' | href='+(a.getAttribute('href')||'').slice(0,150)+' | onclick='+(a.getAttribute('onclick')||'').slice(0,150));
    });
  });
  return JSON.stringify(items.filter(function(x,i,arr){return arr.indexOf(x)===i;}).slice(0,150));
})();
"""


def run_dump(ctx):
    try:
        return json.loads(ctx.run_js(DUMP_JS) or "{}")
    except Exception as e:
        return {"error": repr(e)}


def print_dump(where, d):
    out(f"\n---- {where} ----")
    if d.get("error"):
        out(f"  dump failed: {d['error']}")
        return
    out(f"  url: {re.sub(r';jsessionid=[^?#]*', '', d.get('url', ''))}  title: {d.get('title')!r}")
    if d.get("header"):
        out(f"  header text: {d['header']!r}")
    if d.get("iframes"):
        out(f"  iframes: {d['iframes']}")
    for s in d.get("selects", []):
        out(f"  SELECT id={s['id']!r} name={s['name']!r} label={s['label']!r} visible={s['visible']} "
            f"multiple={s['multiple']} n={s['n']} selected={s['selected']} onchange={s['onchange']!r}")
        for v, t, sel in s["options"]:
            out(f"      {'*' if sel else ' '} {v!r} = {t!r}")
    for i in d.get("inputs", []):
        out(f"  INPUT {i}")
    for t in d.get("texts", []):
        out(f"  TEXT {t['tag']} id={t['id']!r} text={t['text']!r} href={t['href']!r} "
            f"onclick={t['onclick']!r} visible={t['visible']}")
    if d.get("globals"):
        out(f"  window globals: {d['globals']}")
    out(f"  cookie names: {d.get('cookies')}")
    if d.get("dropdowns"):
        out(f"  dropdown toggles: {[(x['id'], x['text'], x['href'], x['visible']) for x in d['dropdowns']]}")


def dump_all(page, where, shot):
    print_dump(f"{where} (top document)", run_dump(page))
    try:
        frames = page.get_frames()
    except Exception as e:
        frames = []
        out(f"  get_frames failed: {e!r}")
    for i, fr in enumerate(frames):
        print_dump(f"{where} (iframe #{i})", run_dump(fr))
        try:
            save_text(f"{shot}_iframe{i}.html", fr.html)
        except Exception:
            pass
    try:
        save_text(f"{shot}.html", page.html)
    except Exception:
        pass
    save_debug_screenshot(page, PROBE_DIR, f"{shot}.png")
    return frames


def open_header_dropdowns(page, shot):
    """Opens each header dropdown toggle (href '#' / empty / javascript only), logs the
    visible menu items, screenshots, closes it. Never follows a link."""
    d = run_dump(page)
    for n, dd in enumerate(d.get("dropdowns", [])):
        href = (dd.get("href") or "").strip().lower()
        if not dd.get("visible") or not (href in ("", "#") or href.startswith("javascript")):
            continue
        try:
            page.run_js(f"var e=document.getElementById({json.dumps(dd['id'])}); if (e) e.click();")
            time.sleep(1.5)
            items = json.loads(page.run_js(VISIBLE_MENU_JS) or "[]")
            out(f"\n  dropdown {dd['text']!r} -> {len(items)} visible menu items:")
            for it in items:
                out(f"      {it}")
            save_debug_screenshot(page, PROBE_DIR, f"{shot}_dropdown{n}.png")
            page.run_js(f"var e=document.getElementById({json.dumps(dd['id'])}); if (e) e.click();")
            page.run_js("document.body.click();")
            time.sleep(0.8)
        except Exception as e:
            out(f"  dropdown {dd.get('text')!r}: {e!r}")


def click_menu_text(page, text):
    js = f"""
        function f(t){{ var els=document.querySelectorAll('a,span,li');
            for (var e of els) {{ if ((e.innerText||e.textContent||'').trim()===t) return e; }} return null; }}
        var d=f({json.dumps(text)}); if (d) {{ d.click(); return 'clicked'; }}
        var s=f('Sales'); if (s) s.click(); return 'opened Sales';
    """
    r = page.run_js(js)
    if r != "clicked":
        time.sleep(2)
        r = page.run_js(js)
    return r


def vinculum_part():
    out("\n" + "=" * 70)
    out("PART B - Vinculum UI: location / warehouse selectors (read-only)")
    out("=" * 70)
    if not (LOGIN_ID and PASSWORD):
        out("  VIN_LOGIN_ID / VIN_PASSWORD not set - skipped.")
        return
    page = launch_browser(os.path.join(PROBE_DIR, "downloads"))
    try:
        page.get(f"{BASE}/eRetailLogin.action?popup=true")
        time.sleep(4)
        dump_all(page, "LOGIN PAGE (before login)", "01_login_page")

        vinculum_login(page, BASE, LOGIN_ID, PASSWORD, ORG_ID)
        time.sleep(3)
        home_url = page.url
        dump_all(page, "HOME (right after login, before closing popups)", "02_home_raw")
        dismiss_top_modals(page, "after login")
        dump_all(page, "HOME", "03_home")
        open_header_dropdowns(page, "03_home")

        page.get(f"{BASE}/reportPickPackBS")
        time.sleep(5)
        dismiss_top_modals(page, "on Pick Pack")
        dump_all(page, "PICK PACK REPORT", "04_pickpack")

        page.get(f"{BASE}/displayCategoryAnalysisBS?posMenu=false")
        time.sleep(5)
        dismiss_top_modals(page, "on Sales report")
        dump_all(page, "SALES REPORT (category analysis)", "05_sales")

        page.get(home_url)
        time.sleep(5)
        dismiss_top_modals(page, "home again")
        out(f"\n  Order Enquiry menu: {click_menu_text(page, 'Order Enquiry')}")
        time.sleep(6)
        dismiss_top_modals(page, "on Order Enquiry")
        frames = dump_all(page, "ORDER ENQUIRY", "06_order_enquiry")
        # The advance-search panel only expands (no search is run).
        for ctx in [page] + list(frames):
            try:
                r = ctx.run_js("""
                    var els=document.querySelectorAll('a,button,span,div');
                    for (var e of els){ var t=(e.innerText||'').trim();
                        if (t==='Advance Search' || t==='Advanced Search'){ var r=e.getBoundingClientRect();
                            if (r.width>0 && r.height>0){ e.click(); return 'clicked'; } } }
                    return 'none';""")
            except Exception:
                r = "err"
            if r == "clicked":
                out("\n  'Advance Search' panel opened (no search run).")
                time.sleep(3)
                dump_all(page, "ORDER ENQUIRY + ADVANCE SEARCH", "07_order_enquiry_advance")
                break
    finally:
        try:
            page.quit()
        except Exception:
            pass


def main():
    os.makedirs(PROBE_DIR, exist_ok=True)
    drive_part()
    try:
        vinculum_part()
    except Exception as e:
        out(f"\n[✗] Vinculum probe stopped: {e!r}")
        sys.exit(1)
    out("\n[✓] Probe finished. Screenshots + HTML in the 'vinculum-location-probe' artifact.")


if __name__ == "__main__":
    main()
