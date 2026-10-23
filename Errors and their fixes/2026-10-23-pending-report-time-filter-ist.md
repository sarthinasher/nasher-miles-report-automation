# Order Enquiry / SKU Master "requested before this run" filter let through reports up to ~5.5 h old

- **Date:** 2026-10-23
- **Reported as:** found in the 23-Oct code + workflow health review ("something is broken"; review item "date handling around month ends and IST"). No wrong file has been traced to it yet.
- **Symptom:** none visible in the logs - a latent hole. The Pending Report grid shows request times in IST (Pick Pack run 39925372015 logged `2026-10-23T08:33:31Z ... '1893398 SUCCESS 23/10/2026 12:47 PM PickPackReport ...'` and `08:34:17Z ... '1894056 SUCCESS 23/10/2026 02:03 PM ...'`, i.e. 14:03 IST = 08:33 UTC). The scripts' `parseTs` read that text as browser-local time, and the CI browser runs in UTC, so `23/10/2026 02:03 PM` became `2026-10-23T14:03:00Z`, 5 h 30 min in the future.
- **Where:**
  - `scripts/order_enquiry_report_to_drive.py` step 9 `get_latest_report` (`if (reqTs && reqTs < (run_start - 60000)) continue;`)
  - `scripts/sku_master_report_to_drive.py` step 8 `get_latest_report` (same code; SKU Master has no other stale-report guard)
- **Root cause:** `parseTs` returned `new Date(y, mo-1, d, hh, mm).getTime()`, which interprets the IST wall-clock text in the browser's own time zone (UTC on the runner), so every request time was 5.5 h too late and no report requested in the last ~5.5 h was ever skipped as "older than this run".
- **Why it wasn't caught:** the filter only matters when this run's own request is not yet in the grid; normally it is (highest id wins), and Order Enquiry also has `_seen_report_ids` for reports of the same process. Nothing tested the JS, and the code was written on a machine in IST, where the bug does not show.
- **Fix:** one shared `common/vinculum_ui.PARSE_IST_TIMESTAMP_JS`: `Date.UTC(y, mo-1, d, hh, mm) - 330 min` (Vinculum shows IST; India has no DST), the same in any browser time zone. Both scripts insert it instead of their own copy. The surrounding logic is unchanged. What it now protects against: an Order Enquiry run whose export request did not register picking up a SUCCESS OrderEnquiryExport of an earlier run (e.g. the 04:35 run's file seen by a manual back-fill a few hours later), and SKU Master doing the same with an earlier SkuEnquiryExport.
- **Evidence:**
  - Old parser under node, `TZ=UTC`: `parseTs("23/10/2026 02:03 PM")` -> `2026-10-23T14:03:00.000Z` (should be 08:33Z).
  - `python -m pytest tests/test_pending_report_time.py -q` -> `4 passed` (TZ = UTC, Asia/Kolkata, America/New_York all give 08:33Z; `01/11/2026 12:10 AM` -> 31-Oct 18:40Z across the month end; scripts no longer contain `new Date(y, mo-1, d, hh, mm`).
  - Full suite: see the commit's test run (`python -m pytest tests -q`).
- **Blast radius:** Order Enquiry and SKU Master downloads only. Pick Pack / Sales / Inventory do not parse request times (they use row text / ids).
- **Still open:** Inventory has no "before this run" guard at all for its first location - see 2026-10-23-inventory-first-location-stale-report.md.
- **Related:** 2026-10-10-order-enquiry-export-fields-3-of-39.md (where step 9 was written), 2026-10-23-inventory-first-location-stale-report.md.
