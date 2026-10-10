# Order Enquiry report missing from Drive on some days ("Fields not fully selected", 3/39)

- **Date:** 2026-10-10
- **Reported as:** "the daily Vinculum "Order Enquiry" report stopped reaching Drive, so the GT Full Flow FMS can't fill Order No / Team / Invoice etc. for orders after 7 Oct."
  Actions log of the failing runs (37612895860 on 2026-10-07, 37923794967 on 2026-10-09), verbatim:
  ```
  Select fields (try 1) -> OK|3/39 || headerNative || sample=Order No,Extern OrderNo,Order Date,Order Type,Status,Order Amount,Voucher Code,Voucher Amount
  Select fields (try 2) -> OK|3/39 || headerNative || sample=...
  Select fields (try 3) -> OK|3/39 || headerNative || sample=...

  [✗] Fields not fully selected — export would be rejected.
  ##[error]Process completed with exit code 1.
  ```
- **Symptom:** `OrderEnquiry_06-10-2026` and `OrderEnquiry_08-10-2026` never appeared in Drive folder
  `1rinwTyd3BseS7o_FHP4Y907egQz39zBO` (05 and 07 Oct did). Same 3/39 signature on 2026-08-24 and
  2026-08-25 (runs 32811468316, 32932690165). Other intermittent failures of the same job in its history:
  "Could not locate the export dialog" (35587099974), "Could not find the Order Enquiry grid"
  (35502195899), "Report did not download within 180s" (29809380622), Drive upload (30522210345).
  A failed day was simply lost: the job only ever exported "yesterday" and had no retry.
- **Where:**
  - `scripts/order_enquiry_report_to_drive.py:493-501` (old) — `native_click()` does
    `el.click(by_js=False)` (a CDP mouse event at the box's screen coordinates) inside
    `try/except: pass` and ignores the return value.
  - `scripts/order_enquiry_report_to_drive.py:511-520` (old) — `select_all_export_fields()` clicks the
    header "select all" box, then every still-unchecked row, only via `native_click()`.
  - `scripts/order_enquiry_report_to_drive.py:529-552` (old) — 3 tries of the same thing, then `fail()`.
  - `scripts/order_enquiry_report_to_drive.py:203` (old) — date hard-wired to "yesterday", so a lost
    day could not be re-run.
- **Root cause:** Field selection was the only step that depended on coordinate (mouse-event) clicks,
  and on some runs those clicks reached nothing — every one of the 37 clicks per try (header + 36 rows)
  left the jqGrid `dynamicFieldGrid` at its 3 locked defaults — while the failure was swallowed, so all
  3 tries repeated the same no-op and the job exited without exporting.
  What intercepted the clicks on those runs was **not captured** (no screenshot, and stdout was
  buffered so there is no per-line timing). Evidence for "clicks dispatched but not landing" rather
  than "clicks skipped": the failing script runs lasted 94 s / 114 s, i.e. ~20 s per try — consistent
  with 37 dispatched clicks per try, not with DrissionPage's 1.5 s give-up per click (~60 s per try).
  The prime suspect is one of the hidden Bootstrap modals in Vinculum's top-level page that Vinculum
  opens on its own and that cover the whole page with a z-index-1050 backdrop:
  `#screenNotificationDialog` ("Remind me after 1 Hour … 1 Day", `dontShowThisNotificationAgain()`,
  opened by `openNotificationDialog`/`notifyScreen`) and `#feedbackFormDialog` ("Take the Survey /
  Not Now"). Coordinate clicks would land on such a backdrop; JS clicks (used by every other step:
  Search, Export, date, Order Source) would not be affected — matching the failure exactly.
- **Why it wasn't caught:** `native_click()` swallowed exceptions and ignored DrissionPage's `False`
  return; the retry loop retried the identical method; no screenshot on failure; Python stdout was
  block-buffered in Actions (all lines carry the exit timestamp); a failed day had no re-run path and
  no second scheduled attempt, so it silently became a permanent gap.
- **Fix:**
  1. Select the fields with `element.click()` executed inside the export iframe on each unchecked row
     box (fires jqGrid's own handler; independent of overlays, geometry, and of rows scrolled outside
     the 380 px-high iframe). Never click the header box — it is a toggle (verified: clicking it when
     all 40 are checked clears back to 3). Verify both the checkbox count AND jqGrid's `selarrrow`
     (39/39) before exporting; the old "never export partial fields" guard is kept. Native clicks are
     kept only as a logged fallback, then any open top-level popup is closed and the pass repeats.
  2. Close any open top-level Vinculum modal after login, on the Order Enquiry screen and before field
     selection (top document only — the export dialog lives in an iframe), logging what was closed.
  3. Dialog Export button: if the dialog is still open after the native click, fire the button's
     click handler by JS; fail if it still does not close (previously reported success blindly).
  4. Each report date gets 2 attempts with a fresh browser (`co.auto_port()`), a failure screenshot
     (uploaded as the `order-enquiry-debug` artifact) and an end-of-run summary; exit 1 if any date
     failed.
  5. Dates: `workflow_dispatch` input `report_date` (DD/MM/YYYY, a comma list, or a range
     DD/MM/YYYY-DD/MM/YYYY; `common/report_dates.py`); file name always uses the REPORT date. Scheduled
     runs (now twice a day, 04:35 and 09:35 UTC) use a catch-up window — the last 3 days ending
     yesterday (IST) whose file is missing in Drive — so a failed or late run is repaired by the next.
  6. `concurrency: order-enquiry-report` (two logins with the same Vinculum user kill each other's
     session — reproduced: run 38033672615 failed with "Could not locate the export dialog" when a
     second run logged in 5 s after its Export click), `PYTHONUNBUFFERED=1`, pytest step.
- **Evidence:**
  - Diagnostic runs on a throw-away branch (`diag/oe-field-select`, deleted) dumped the dialog:
    `dynamicFieldGrid` multiselect, 39 rows, `sel: 3` before; JS `click()` on the 36 unchecked rows ->
    `{"total":40,"checked":39}` and `jqgrid sel: 39`; JS click on header when all checked ->
    `before=true after=false`, 3 checked (toggle). Top page holds `screenNotificationDialog` and
    `feedbackFormDialog` (both `modal fade`, z=1050, hidden at the time). 6 diagnostic runs (incl.
    08/10 and 06/10, a deliberately overlapping login, and a 150 s hold) never reproduced 3/39.
  - `python -m pytest tests -q` -> `17 passed in 0.02s`.
  - Verification run 38034469012 (fix branch, `report_date=06/10/2026`):
    ```
    Fields before: 3/39 checked (jqGrid selected 3/39)
    Select fields (try 1) JS-click 36 boxes -> 39/39 checked, jqGrid selected 39/39
    [✓] All 39 export fields selected.
    ...
    [OK] Uploaded new Google Sheet to Drive: OrderEnquiry_06-10-2026
    OrderEnquiry_06-10-2026: OK (attempt 1)
    ```
    Drive file `1Alel87Xw_yUIvdy0OikmIRnSQdXipbcO2dFcsyeP_yE` has all 39 columns
    (`Order No,Extern OrderNo,Order Date,…,Distribution Type`) and rows dated `06/10/2026`.
  - Back-fill from main after commit d7b3201: run 38034642700 (`report_date=08/10/2026`) ->
    `OrderEnquiry_08-10-2026: OK (attempt 1)`; run 38034754595 (blank = catch-up) ->
    `Already in Drive (skipped): OrderEnquiry_07-10-2026, OrderEnquiry_08-10-2026` /
    `OrderEnquiry_09-10-2026: OK (attempt 1)`.
  - GT Full Flow dry-run after the back-fill (nasher-fms-automation run 38034854973):
    `Combined Order Enquiry rows: 101,175` (unchanged), `matched now … 0`, October Order No still
    `51` of `178` open rows — confirming the Full Flow does not read these files (see Blast radius).
- **Blast radius:** Only `order_enquiry_report_to_drive.py` and its workflow. `common/drive_utils.py`
  unchanged. `scripts/sku_master_report_to_drive.py` has the same `native_click` header/row code and
  can fail the same way (weekly job, last failure 2026-08-31 was a session expiry) — not changed here.
  Downstream: nothing found in the local repos or in Apps Script visible to this account reads
  `OrderEnquiry_*` files. NOTE: the "Vinculum Sales Master" (`1Jnt0yU…`, read by the GT Full Flow FMS
  as its "Order Enquiry" source) is filled by Apps Script `VinculumSalesReportConsolidate`
  (`syncVinRetailReports`, daily 8 AM trigger) from the **Vinculum Sales Report** folder
  `1iL94fl0nUOxZ_pEWH_AuxnqBYsYjsjt_` (tab "Sales Report", grouped by Invoice Date) — not from this
  Order Enquiry folder. So this failure does not explain the Full Flow's October Order No gap.
- **Still open:** (1) Which popup (if any) blocked the clicks on 07/09 Oct is unconfirmed — the next
  occurrence will log `Closed Vinculum popup(s) …` and/or save a screenshot artifact. (2) The other
  Vinculum jobs in this repo log in with the same user on overlapping-ish schedules; only this workflow
  is serialized. (3) ~~`sku_master_report_to_drive.py` still uses the old click method.~~ Fixed — see
  `2026-10-10-sku-master-fragile-clicks.md` (shared helpers in `common/vinculum_ui.py`, shared
  `vinculum-session` concurrency group). (4) Earlier lost
  days (e.g. Aug/Sep failures) are outside the 3-day catch-up window — back-fill manually with
  `report_date` if they are needed.
- **Related:** none in this folder (first record).
