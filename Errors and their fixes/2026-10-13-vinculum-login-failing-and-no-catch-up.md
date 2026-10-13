# Every Vinculum report job fails at login since 12-Oct; Pick Pack / Sales days are lost for good

- **Date:** 2026-10-13
- **Reported as:** "check if all the files are being fetched correctly and if it's working fine end to end — all the details should be updated at least within 24 hours." Job logs: `[✗] Login failed — check VIN_LOGIN_ID / VIN_PASSWORD secrets.`
- **Symptom:** Pick Pack (run 38238723052, 12-Oct 10:49 UTC), Sales Report (38241574914), Inventory
  (38245265578), Order Enquiry (38248257188, 38296513054) and SKU Master (38275516283, 38310553764)
  all failed at login; the debug screenshot shows the login page with user `Diwakar` and an empty
  password, no message. Last good files in Drive: `PickPackReport_10.10.2026`, `10.10.2026`,
  `OrderEnquiry_10-10-2026` (created 11-Oct). The SharePoint sync (38245043897) then uploaded the
  newest Drive file `10.10.2026` as `Sales/Vinculum/11.10.2026.xlsx`. Pick Pack `27.09.2026` has been
  missing since September.
- **Where:**
  - login block copied into every script, e.g. `scripts/pickpack_report_to_drive.py:84-117`
    (old), `scripts/order_enquiry_report_to_drive.py:129-161` (old): `page.handle_alert(accept=True)`
    discards the alert text; password injected as `'{PASSWORD}'` into JS.
  - `scripts/pickpack_report_to_drive.py` / `scripts/vinculum_sales_report_to_drive.py`:
    `DATE_OPTION = "Yesterday"`, file named `datetime.today() - 1 day` (runner UTC date).
  - `scripts/sync_sales_to_sharepoint.py:83` `get_newest_drive_file` + name from yesterday.
  - `.github/workflows/pickpack-report.yml`, `vinculum-sales-report.yml`, `inventory-report.yml`:
    no `vinculum-session` concurrency group, one run a day.
- **Root cause:** (1) Vinculum stopped accepting the stored login between 11-Oct 15:55 UTC (last
  success) and 12-Oct 10:49 UTC; the reason Vinculum gave was accepted and thrown away by
  `handle_alert`, so the logs cannot say whether the password expired, was changed, or the user was
  disabled (the user is `Diwakar`, whose work is being handed over). (2) Pick Pack and Sales exported
  only "Yesterday", so any failed / skipped run lost that day permanently. (3) The SharePoint sync took
  "the newest file" and stamped it with yesterday's date, so a missing export became wrong data.
- **Why it wasn't caught:** The failing runs did go red, but every job retried the login (Order
  Enquiry 4 logins per run, SKU Master 2) without new information, and nothing downstream checked
  file freshness (Full Flow kept running green on 3-day-old data — see nasher-fms-automation
  `2026-10-13-full-flow-data-older-than-24h.md`).
- **Fix:**
  - `common/vinculum_ui.py` `vinculum_login`: one shared login for all 5 scripts; logs Vinculum's
    alert / on-page message with a hint (expired / locked / wrong password); raises `LoginError`,
    which is never retried and stops the rest of the run (repeated failed logins can lock the user);
    values passed with `json.dumps` (a `'` or `\` in a password broke the old JS).
  - `common/daily_export.py` + `common/report_dates.py` `dates_to_export`: Pick Pack and Sales
    Report now export every missing day of the last 7 (catch-up, like Order Enquiry), or the
    `report_date` input (one / list / range) for back-fills such as `27/09/2026`; files named from
    the REPORT date (`PickPackReport_DD.MM.YYYY`, `DD.MM.YYYY`), not the runner's UTC date. The date
    is set with the same daterangepicker call Order Enquiry uses (`set_report_date`), falling back to
    the old "Yesterday" click for yesterday only. A new pending report is required (earlier report
    rows/ids are ignored).
  - Order Enquiry catch-up window 3 → 7 days.
  - Workflows: Pick Pack, Sales Report and Inventory join `concurrency: vinculum-session`; Pick Pack
    and Sales get a second (catch-up) cron, `report_date` / `skip_existing` inputs, tests before the
    run, failure screenshots as artifacts, 60-min timeout.
  - SharePoint sync mirrors the file NAMED for yesterday (or `report_date`), replaces a same-named
    file, fails if it is missing; second cron.
  - `requirements.txt` pinned to the versions of the last green runs.
- **Evidence:** `python -m pytest tests -q` → `51 passed` (30 before; new: `tests/test_daily_export.py`
  16, `tests/test_report_dates.py` +5, window test updated to 7 days), same result in a clean
  venv with the pinned requirements (`pip check`: No broken requirements found). Live: see the
  manually dispatched Pick Pack run in the report to the user (one login attempt).
- **Blast radius:** Drive folders read by GT Full Flow FMS (Pick Pack `1Xi66o…`, Sales `1iL94fl…`,
  Order Enquiry `1rinwTy…`) and by the Apps Script Sales Master consolidation (`1iL94fl…`): catch-up
  adds missing days with the usual names, never renames or deletes. SharePoint `Sales/Vinculum`.
  With five workflows in `vinculum-session`, GitHub keeps only ONE waiting run per group and cancels
  an older waiting one — Pick Pack / Sales / Order Enquiry recover through catch-up, Inventory (a
  snapshot) could miss a day.
- **Live check after the fix (run 38364136982, Pick Pack catch-up, 13-Oct):** one login attempt,
  then stop: `Vinculum login failed for user '***': no message shown, still on the login page`;
  12.10 marked `NOT RUN`. The current login page (fetched 13-Oct) RSA-encrypts the password in
  `doLoginJS()` (`#password` -> `encryptPasswordRSAUsingPubKey` -> hidden `name="password"`); the
  script's submit click runs that same `doLoginJS()`, so the page flow is unchanged for it. Vinculum
  shows no reason on a failed login, which points to the stored credentials (user `Diwakar`, whose
  work is being handed over) no longer being accepted.
- **Still open:**
  - The login itself: needs a valid Vinculum user/password in the repo secrets `VIN_LOGIN_ID` /
    `VIN_PASSWORD` (ideally a service user, not a person's account). Nothing in code can fix this.
    Check first by logging in by hand at nashermiles.vineretail.com with the stored user.
  - `Sales/Vinculum/11.10.2026.xlsx` on SharePoint holds 10-Oct invoices: re-run "Sync Sales to
    SharePoint" with `report_date` `11/10/2026` once `11.10.2026` exists in Drive (it replaces it).
  - The custom-date path (`set_report_date`) is proven on the Order Enquiry page only; the first
    back-fill run of Pick Pack / Sales shows whether it also works there (else: failure screenshot
    artifact; yesterday still works through the old path).
- **Related:** `2026-10-10-order-enquiry-export-fields-3-of-39.md`,
  `2026-10-10-sku-master-fragile-clicks.md`.
