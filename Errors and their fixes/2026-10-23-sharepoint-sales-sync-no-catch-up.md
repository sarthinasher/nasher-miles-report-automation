# Sync Sales to SharePoint failed every day 13-22 Oct; 12.10-21.10 never reached SharePoint and 11.10.2026.xlsx holds 10-Oct invoices

- **Date:** 2026-10-23
- **Reported as:** "Audit and fix the 'Sync Sales to SharePoint' job ... The sync failed on 22-Oct (runs 39754365332 and 39795921554) and succeeded today (39930389635) ... The user suspects something is broken." Exact log text of both 22-Oct runs:
  ```
  [2] Finding 21.10.2026 in the Vinculum Sales Drive folder...
      [!] 21.10.2026 is not in Drive yet (Vinculum Sales Report export missing) — not synced.
  [✗] Nothing to sync — no file for the requested date(s).
  ##[error]Process completed with exit code 1.
  ```
- **Symptom:**
  - Every scheduled sync from 13-Oct (38391407187) to 22-Oct (39795921554), 20 runs, failed the same way for "yesterday" (12.10 ... 21.10). Today's 39930389635 uploaded only `22.10.2026.xlsx` (369094 bytes, Drive file modified 2026-10-23T06:26:19Z).
  - The Drive files for those days appeared later than the day's two sync runs: e.g. `21.10.2026` created 2026-10-22T15:49Z (after the 14:28Z sync), `19.10.2026` created 2026-10-21T15:50Z, `17.10.2026` created 2026-10-22T06:38Z; `11.10`, `12.10`, `20.10`, `22.10` were only created by today's back-fill 39909075764 (`report_date=11/10/2026-22/10/2026`, 06:02Z). Nothing ever went back for them.
  - 12-Oct run 38245043897 (old "newest file" code): `Newest file: 10.10.2026 (modified 2026-10-11T10:18:15.575Z)` -> `Downloading/exporting to 11.10.2026.xlsx` -> uploaded. SharePoint `11.10.2026.xlsx` = 10-Oct invoices.
  - The same old code mislabelled earlier days too (newest file != yesterday): SharePoint 28.07/29.07 (= 27.07 data), 04.08/05.08 (= 29.07), 12.08/13.08 (= 11.08), 19.08 (= 18.08), 20.08-23.08 (= 29.06 data!), 02.09 (= 01.09), 06.09 (= 05.09), 19.09 (= 18.09).
- **Where:**
  - `scripts/sync_sales_to_sharepoint.py:76` (before): `return requested or [today - timedelta(days=1)]` - a scheduled run looks at yesterday only; `:110` `if not found: fail(...)`.
  - `scripts/sync_sales_to_sharepoint.py:125-127` (before): download then upload with no check of the content.
  - `common/sharepoint_utils.py:47,67,75,104,125,132` (before): bare `raise_for_status()`, no retry anywhere; `:90-101` a folder is (re)created after ANY non-200 lookup with `conflictBehavior: "replace"`.
- **Root cause:** the sync mirrored only "yesterday" and the Sales export for a day kept landing after that day's two sync runs (Vinculum report ERROR / timeouts / Drive SSL EOF on 12-22 Oct, then a back-fill), so each late file was never looked at again; the job had no catch-up window, unlike the export jobs (`DEFAULT_LOOKBACK_DAYS = 7` since 2026-10-13).
- **Why it wasn't caught:** the run went red every day, but the red only said "not in Drive yet" for the newest day; nothing reported that the earlier days were still missing on SharePoint. No test covered more than `sync_dates(..)== [yesterday]`. There was also no content check, so had a broken file been present at sync time (the 1-column / ~1.5 KB Sales files of 20/21-Oct for 14/15/16/18.10, see 2026-10-21 record) it would have been mirrored as is.
- **Fix:**
  - `scripts/sync_sales_to_sharepoint.py`
    - blank `REPORT_DATE` = catch-up of the 7 days ending yesterday (IST) (`sync_dates`, `default_window`). Per day (`sync_reason`): upload if not on SharePoint, or if the Drive file's `modifiedTime` is later than the SharePoint copy's `lastModifiedDateTime` (re-exported since); skip if up to date; a day missing in both is reported. SharePoint folder listed once (`list_folder_files`).
    - explicit `REPORT_DATE` (one / list / range) = always re-upload those days.
    - content guard before every upload (`validate_sales_file` / `check_sales_rows`): reads the `Sales Report` sheet (`reset_dimensions()`), refuses an empty / 0-byte / unreadable file, < 10 named columns (the 1-column "Order No" files), a missing `Order No` / `Invoice Date` column, header-only (0 data rows), or a file with no invoice dated that day (catches "10.10 data under 11.10"). A refused day leaves SharePoint unchanged and fails the run with the reason.
    - one day's failure no longer stops the others; Drive lookup / download go through `with_drive_retries`; summary lists every date not synced; exit 1 if any.
  - `common/sharepoint_utils.py`
    - `graph_request`: every Graph call retried on 429 / 500 / 502 / 503 / 504 and dropped / timed-out connections (5 attempts, 2-4-8-16 s + jitter, `Retry-After` honoured, cap 60 s); a 401 renews the token once and repeats the call.
    - `GraphAuth`: token cached with its `expires_in`, renewed 5 min before expiry; `fetch_token` retries 5xx and turns a refused request into `SharePointAuthError` with Azure AD's reason and a hint (AADSTS7000222 expired secret, 7000215 wrong secret, 700016 wrong client id, 90002 tenant). The secret is never printed; upload-session URLs' query strings (`tempauth`) are masked in errors.
    - `ensure_folder_path`: creates only after a 404, with `conflictBehavior: "fail"` (409 = exists = fine). Before, a transient 5xx on the existing `Sales/Vinculum` lookup would have POSTed a same-named folder with "replace".
    - uploads: simple PUT and every upload-session chunk retried; chunks sent without an Authorization header (pre-authenticated URL); a 0-byte result raises.
  - `.github/workflows/sync-sales-to-sharepoint.yml`: input text (blank = catch-up), `concurrency: sync-sales-to-sharepoint`, `PYTHONUNBUFFERED=1` (the log printed every step at the same second), timeout 10 -> 20 min.
  - Tests: `tests/test_sharepoint_sync.py` (34): catch-up window, re-export detection, explicit re-upload, the 22-Oct and 23-Oct situations end to end with faked Drive/SharePoint, broken file not mirrored, one-column / empty / header-only / wrong-day / missing-column refusals, Graph retries (503, ConnectionError, 429 Retry-After, give-up, 403 not retried), 401 token renewal, renewal before expiry, AADSTS7000222 message, folder never replaced, listing pagination, simple and chunked upload. Old `test_sync_dates_default_yesterday_and_backfill` (asserted yesterday-only) removed from `tests/test_daily_export.py`.
- **Evidence:**
  - `gh run view 39754365332 --log-failed` / `39795921554`: the lines quoted above. `gh run list -w sync-sales-to-sharepoint.yml`: 20 failures 2026-10-13T11:36Z ... 2026-10-22T14:28Z, each "Finding <yesterday> ... not in Drive yet"; no upload between 12-Oct and 23-Oct.
  - `gh run view 39930389635 --log`: `[5] 22.10.2026 (modified 2026-10-23T06:26:19.467Z) -> 22.10.2026.xlsx` / `Local file ready: /tmp/sp_sync/22.10.2026.xlsx (369094 bytes)` / `Synced: 22.10.2026`.
  - `python -m pytest tests/test_sharepoint_sync.py -q` -> `34 passed`; `python -m pytest tests -q` -> `154 passed, 9 warnings`.
- **Blast radius:** only this job writes `Sales/Vinculum` on SharePoint (no other code in this repo calls Graph). Readers of `Sales/Vinculum/DD.MM.YYYY.xlsx` (finance consolidation) saw no files for 12.10-21.10 and 10-Oct data under 11.10. `common/sharepoint_utils.py` is used only by this script.
- **Still open:**
  - SharePoint back-fill is not run by this change: `gh workflow run sync-sales-to-sharepoint.yml -R Shreyash-Thakur/nasher-miles-report-automation -f report_date=11/10/2026-21/10/2026` (16-21.10 would also be caught by the next scheduled run; 11-15.10 are outside the 7-day window).
  - Older mislabelled SharePoint files (list above). Drive now has the right file for 28.07, 29.07, 12.08, 13.08, 19.08, 23.08 (re-sync with report_date; check first that nobody corrected them by hand on SharePoint). 04.08, 05.08, 20-22.08, 02.09, 06.09, 19.09 have no Drive file - need a Sales export first. Drive 02.10 (4 data rows), 14.09, 15.08 (~9 KB) look partial - re-export, then re-sync.
  - Drive `20.10.2026` has only 164 rows (A02) vs 500-850 on other days - worth a look in Vinculum.
  - GitHub's schedule runs this job hours late (cron 04:05 / 09:05 UTC; runs at ~06:30 UTC in July, ~09:15 / ~14:28 UTC now), so the first run usually comes before the Sales export uploads; the catch-up makes that harmless. Cron not changed.
- **Related:** 2026-10-13-vinculum-login-failing-and-no-catch-up.md (same catch-up idea for the exports; sync by name), 2026-10-21-merged-order-enquiry-and-sales-files-only-order-no-column.md (the 1-column files), 2026-10-23-drive-upload-ssl-eof.md (why 20/21.10 uploads failed), 2026-10-23-sales-17-location-loop-timeouts.md.
