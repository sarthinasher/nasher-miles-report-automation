# Vinculum Sales Report workflow fails every run since 20-Nov: "Site Location is not H01 after switching location"

- **Date:** 2026-11-27
- **Reported as:** Vinculum Sales Master and the Daily Sales Report (Vijay) stale since 19-Nov; GitHub Action "Vinculum Sales Report to Drive" failing.
- **Symptom:** 32 runs in a row failed since 20-Nov 12:05Z (last success 19-Nov 17:36Z). Login works ("Session location after login: H01"), then step 4 stops:
  `Site Location on screen: -1 (options: -1,A03,A06,...)` followed by "The MIS report's Site Location is not H01 after switching location."
- **Where:** `scripts/vinculum_sales_report_to_drive.py:337` (step 4) calling `check_site_location()` in `common/vinculum_ui.py:778`.
- **Root cause:** The MIS Reports page's Site Location select (`#siteLoc`, `reportDTO.siteLoc`) now loads unset (`-1`, ~200 options) instead of preselected to the session location, and the strict `value == loc` check treated "unset" as "wrong location".
- **Why it wasn't caught:** Tests only covered the pure helpers, never the Site Location select; the page behaviour changed on Vinculum's side (same 19/20-Nov change window as the unswitchable locations).
- **Fix:** New `ensure_site_location()` + `SET_SITE_LOCATION_JS` in `common/vinculum_ui.py`. If the select reads `-1`/blank and `loc` is among its options, it sets the value, fires `input`/`change` (and jQuery change), re-reads, and fails only if it is still not `loc` (logs "Site Location was -1 -> set to H01; now reads H01"). `loc` missing from the options, or another real code on screen, still fails (a wrong location is never overridden). Only the Sales script uses it.
- **Evidence:** `python -m pytest tests -q`: 176 passed (7 new tests in `tests/test_vinculum_ui.py`: -1 -> set -> ok; blank -> set; -1 and loc missing -> fail; wrong code -> fail, untouched; already correct -> no change; no select -> None; set does not stick -> fail).
- **Blast radius:** Sales workflow only. `check_site_location` is unchanged and still used by Pick Pack (`scripts/pickpack_report_to_drive.py:220`); Pick Pack / Order Enquiry still succeed and behave as before.
- **Still open:** Not verified live: needs a real workflow run (the real select may also need a page-side trigger we cannot see offline). Backfill of 19-Nov onward is needed after the first good run (Sales Master / Vijay report).
- **Related:** `2026-11-23-vinculum-8-locations-unswitchable.md`.
