# SKU Master export used the same fragile mouse-click field selection that lost Order Enquiry days (preventive fix)

- **Date:** 2026-10-10
- **Reported as:** "`scripts/sku_master_report_to_drive.py` (and its workflow) uses the same old native-click
  code that failed." (preventive — the SKU Master job has not yet failed this way). Its last failure,
  run 33385702682 on 2026-08-31, verbatim:
  ```
  Select fields (try 1) -> OK|48/48 || headerNative || sample=Sku Code,BomType,Sku Name,Style,...
  [✓] Native click on dialog Export.
  ...
  [✗] Session expired mid-run (redirected to login) while waiting for the report.
  ##[error]Process completed with exit code 1.
  ```
- **Symptom:** None yet for field selection. The weekly snapshot for 31-08-2026 is missing from Drive
  folder `1FcWUXpLckpusvBoQUBa66rqQ_b-c0K0o` (session expiry; the job had no retry and no second
  scheduled attempt, so the week was simply lost).
- **Where (before this change):**
  - `scripts/sku_master_report_to_drive.py:490-498` — `native_click()` does `el.click(by_js=False)`
    (a CDP mouse event at screen coordinates) inside `try/except: pass`, ignoring the return value.
  - `scripts/sku_master_report_to_drive.py:500-523` — `select_all_export_fields()` clicks the header
    "select all" box (a TOGGLE) and then each unchecked row, only via `native_click()`.
  - `scripts/sku_master_report_to_drive.py:525-549` — 3 identical tries, then exit.
  - `scripts/sku_master_report_to_drive.py:597-622` — dialog Export native click; "Dialog still open
    after click?" was printed but ignored.
  - `.github/workflows/sku-master-report.yml` — one weekly cron (Mon 04:50 UTC, 15 min after the
    Order Enquiry cron at 04:35), no concurrency group, buffered stdout, no screenshot artifact.
- **Root cause (shared failure mode):** the export-field checkboxes were selected only by coordinate
  (mouse-event) clicks whose failure was swallowed. Anything drawn over the page — Vinculum opens its own
  top-level Bootstrap modals (`#screenNotificationDialog`, `#feedbackFormDialog`, z-index 1050 + a
  full-page backdrop) — makes every such click land on the overlay, so every retry repeats the same no-op.
  That is exactly how the Order Enquiry job got stuck at 3/39 fields on 2026-10-07/09 (see Related);
  the SKU Master script contained the identical code path (copy of the same template), so it would fail
  the same way the first time a popup was open during its weekly run. Separately, both jobs log in as the
  same Vinculum user, and a second login kills the first session — the SKU job ran with no concurrency
  protection and 15 min after the Order Enquiry cron (GitHub routinely delays scheduled runs by hours, so
  actual start times drift into each other).
- **Why it wasn't caught:** the 2026-10-10 Order Enquiry fix deliberately left this script unchanged
  (recorded under "Still open" there); the click helper swallowed errors; no screenshot on failure;
  stdout block-buffered in Actions; a failed week had no re-run path.
- **Fix:**
  1. New shared module `common/vinculum_ui.py` (extracted from the fixed Order Enquiry script, now used by
     both scripts — no copy-paste): `dismiss_top_modals`, `launch_browser` (fresh port/profile),
     `save_debug_screenshot`, `select_all_export_fields` (JS `element.click()` on each unchecked row box
     inside the export-fields iframe, header toggle never clicked, verification of both the checkbox count
     and jqGrid's `selarrrow`, native clicks only as a logged fallback, popup dismissal between tries,
     returns `(ok, state)` — callers refuse to export when not ok), `request_dialog_export` (native click,
     JS fallback if the dialog is still open, fail if it still does not close), and the pure
     `all_fields_selected` / `parse_field_state`.
  2. `sku_master_report_to_drive.py`: popup dismissal after login, on the SKU Master screen and before
     field selection; field selection + dialog Export via the shared helpers; `run_once()` with
     `MAX_ATTEMPTS=2` fresh-browser attempts (30 s pause), a screenshot per failed attempt in
     `/tmp/sku_master_debug`, exit 1 only after all attempts failed. Drive name unchanged
     (`SKU Master DD-MM-YYYY`, run date IST), same folder.
  3. Catch-up for missed weeks: the SKU Master is a snapshot (cannot be exported "as of" a past day), so
     scheduled runs now fire Mon 06:50 UTC, Mon 12:50 UTC and Tue 06:50 UTC, and each only exports if no
     `SKU Master DD-MM-YYYY` dated this week (Mon..today IST) is in Drive (`weekly_file_present` in
     `common/report_dates.py`). Manual runs always export (`skip_existing` input to opt into the check).
  4. Both Vinculum workflows now share `concurrency: group: vinculum-session` (`cancel-in-progress:
     false`), so an SKU Master run and an Order Enquiry run can never be logged in at the same time.
     SKU crons moved off the 03:17-04:35 / 09:35 UTC Vinculum slots.
  5. SKU workflow: `PYTHONUNBUFFERED=1`, pytest step, `sku-master-debug` artifact on failure,
     timeout 15 -> 40 min (2 attempts x up to 240 s report wait).
  6. Order Enquiry script refactored onto the same helpers (behaviour unchanged) and its Drive listing
     moved to `common/drive_utils.list_file_names`.
- **Evidence:**
  - `python -m pytest tests -q` -> `30 passed in 0.05s` (17 existing + 13 new in
    `tests/test_vinculum_ui.py`: the 3/39 state and a checkbox/jqGrid disagreement are rejected,
    unparseable state is never "selected", weekly catch-up decisions, file-name format unchanged).
  - Real `workflow_dispatch` run 38047815009 (branch `fix/sku-master-hardening`, no other Vinculum job
    running), log verbatim:
    ```
    30 passed in 0.09s
        Mode: manual — target file: SKU Master 10-10-2026
        [debug] inner checkboxes=49 tables=3 trs=51 selects=1
        Export Type -> OK|Normal Export
        Fields before: 1/48 checked (jqGrid selected 1/48)
        Select fields (try 1) JS-click 47 boxes -> 48/48 checked, jqGrid selected 48/48
        [✓] All 48 export fields selected.
        [✓] Native click on dialog Export.
        Dialog still open after click? False
    [  8s] SkuEnquiryExport #1809663  Status: SUCCESS
        [OK] Uploaded new Google Sheet to Drive: SKU Master 10-10-2026
      SKU Master 10-10-2026: OK (attempt 1)
    ```
    Drive: `SKU Master 10-10-2026` (`1p6Yy8C8ej5mPu9TFYHFTqN2LXvLdk0uNU4EHISu4tiQ`, 319,373 bytes) in
    folder `1FcWUXpLckpusvBoQUBa66rqQ_b-c0K0o`, next to `SKU Master 05-10-2026` (318,230 bytes).
  - Order Enquiry run 38047378321 (main, before the refactor) back-filled 14/09, 19/09, 20/09 at 39/39
    each; the refactored Order Enquiry code path is the same functions, exercised by the SKU run above.
- **Blast radius:** `scripts/order_enquiry_report_to_drive.py` (imports the shared helpers; concurrency
  group renamed `order-enquiry-report` -> `vinculum-session`), `common/drive_utils.py` (new function
  only), `common/report_dates.py` (new functions only). Downstream readers of the SKU Master folder see
  the same file names; this verification run added an extra Saturday file `SKU Master 10-10-2026`, which
  also means Monday 12-10's scheduled run still exports (new week).
- **Still open:** (1) `inventory-report.yml`, `pickpack-report.yml`, `vinculum-sales-report.yml` also
  log in as the same Vinculum user and are NOT in the `vinculum-session` group (not in scope here). Note
  GitHub keeps at most one *pending* run per concurrency group — a third run arriving while one runs and
  one waits cancels the waiting one — so adding many jobs to one group needs the catch-up logic each job
  now has (OE: 3-day window; SKU: weekly). (2) `grep "by_js=False\|native_click" scripts/*.py`: none of the
  other scripts select export fields this way; `inventory_report_to_drive.py:353` still does one native
  Export-button click (`btn.click(by_js=False)`, reported as success without checking) — not changed here. (3) The 31-08-2026 weekly SKU snapshot cannot be recreated (snapshot report).
- **Related:** [2026-10-10-order-enquiry-export-fields-3-of-39.md](2026-10-10-order-enquiry-export-fields-3-of-39.md)
  (same failure mode, first occurrence; its "Still open" item 3 is closed by this change).
