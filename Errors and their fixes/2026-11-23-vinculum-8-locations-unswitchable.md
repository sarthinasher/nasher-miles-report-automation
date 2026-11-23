# Inventory / Pick Pack / Order Enquiry / Sales report jobs fail every run since 19-Nov: "session location is still ?"

- **Date:** 2026-11-23
- **Reported as:** GitHub mail "[sarthinasher/nasher-miles-report-automation] Run failed: Inventory Report to Drive - main (0e363a3)", "Inventory Report to Drive: All jobs have failed", job run-inventory-report "Failed in 2 minutes and 38 seconds".
- **Symptom:** Every Inventory run since 19-Nov-2026 14:27 UTC failed, 8 in a row; the last good run was 18-Nov 19:06 UTC. Each run logged in, exported H01, then stopped:
  `[!] Switch to A02 (try 1) did not take: session location is still ?` … `[✗] Could not switch the Vinculum location to A02 (session location is still ?).`
  Other jobs failed the same way:
  - Pick Pack: from 20-Nov, on NM8.
  - Order Enquiry: from 19-Nov.
  - Vinculum Sales: from 20-Nov.
  - Sync Sales to SharePoint: from 20-Nov, as a knock-on (no new Sales files).
  As a result, no Inventory snapshot reached Drive for 19-Nov … 23-Nov.
- **Where:**
  - `common/vinculum_ui.py` `switch_location()`: the raise after the "did not take" retries.
  - `plan_locations()` / `export_one_location()`: build the list of locations to export.
  - `resolve_locations()`: decides that list.
- **Root cause:** Since 19-Nov-2026 ~14:00 UTC, Vinculum still lists 8 locations on the Switch Location page (A02, NM2, S02, NM3, NM6, S03, NM8, W03). But a switch to any of them gets a blank location from the server (`orgLocKey` "_NASA" instead of "A02_NASA"), and the session dies (the next page is the login page). Every job switches through all of its locations and aborts at the first broken one.
- **How it was proven (branch `diag/location-switch`, probe workflow, dry runs only, nothing uploaded):**
  - The probe dump of the Switch Location page shows the same form as before: 19 options, session lands on H01, as in the last good run. The page's `doCompanyChange()` posts `selectCompanyEretail` with a new `client` field (VE-1131), which is "-1".
  - Run 45142629641 posted the same switch to A02 three ways:
    - without `client`;
    - with `client=0`;
    - without `client` and without `weighingTemp`.
    All three got HTTP 200 "Welcome to eRetail" with `orgLocKey=_NASA`, and the Switch Location page afterwards showed `current=''`. So the `client` field is not the cause.
  - Run 45143090895: after the A02 switch, the home page redirects to `eRetailLogin.action`, so the session is gone.
  - Run 45143511304: the same code switching to NM4 returns `loggedInLocation=NM4 orgLocKey=NM4_NASA` and the export works ("Real data present: 156 row(s)").
  - Run 45144026366 (`report=switchall`, every location, logging in again after each failure):
    `RESULT A02=BROKEN A03=OK A07=OK H01=OK HO2=OK GJ1=OK NM0=OK NM2=BROKEN NM1=OK NM4=OK S02=BROKEN NM3=BROKEN S01=OK NM6=BROKEN S03=BROKEN NM8=BROKEN NM7=OK NM9=OK W03=BROKEN`
- **Why it wasn't caught:**
  - The jobs treat any switch failure as fatal by design ("a partial merge is never uploaded"), so one broken location stops all of them.
  - The logs only said "session location is still ?", without Vinculum's answer.
  - Nothing tests every location's switch on its own.
- **Fix:**
  - `UNSWITCHABLE_LOCATIONS = "A02,NM2,S02,NM3,NM6,S03,NM8,W03"` with `unswitchable_locations()`. The repo variable `VIN_UNSWITCHABLE` overrides it; "none" turns the skip off. All 4 report workflows and the probe pass it through.
  - `resolve_locations(..., skip=)` drops those codes with a loud `[!] NOT exported - Vinculum lists … but a switch to them blanks the session` line, also when VIN_LOCATIONS / the Pick Pack default names them (NM8). Only "nothing left" is still an error.
  - `plan_locations()` and `export_one_location()` pass the list.
  - `scripts/diag_switch_all.py` (probe `mode=dry_run`, `report=switchall`) re-checks every location, so the list can be trimmed once Vinculum is fixed.
  - Why skip instead of re-login-and-continue: a failed switch kills the session. A fixed, visible list keeps the 11 working locations exporting today and states exactly what is missing. Unknown new breakages still fail loudly, as before.
- **Evidence:**
  - `python -m pytest tests -q`: 168 passed. Four new tests:
    - skipped with a warning, also when named;
    - skip codes the user doesn't have are ignored;
    - only-unswitchable-left is an error;
    - default list and env override.
  - Full Inventory dry run on `fix/skip-unswitchable-locations`, probe run 45146410785:
    `[!] NOT exported - Vinculum lists A02, NM2, S02, NM3, NM6, S03, NM8, W03 …`, then `exporting 11 location(s)`.
    Data rows: H01 14837, A03 759, A07 34, HO2 98, GJ1 880, NM0 9, NM1 3224, NM4 1056, S01 0, NM7 2699, NM9 1213.
    Ended `[dry run] NOT uploaded to Drive; merged file kept as /tmp/vinculum_probe/DRYRUN_23-11-2026_14-53.csv`.
- **Blast radius:**
  - Every script that uses `plan_locations` / `export_one_location`: Inventory, Pick Pack, Order Enquiry, Vinculum Sales (ALL mode).
  - Stock and orders at the 8 skipped locations (incl. NM8 Hyderabad, a GT route) are missing from the Drive files until Vinculum fixes them.
  - SharePoint sales sync recovers by itself once Sales files are back (7-day catch-up).
- **Still open:**
  - The Vinculum admin / helpdesk has to say why the 8 locations stopped switching for this login on 19-Nov (location deactivated? user mapping or client mapping changed?). Then run `switchall` and set `VIN_UNSWITCHABLE` to what is still broken, or "none".
  - The 19–23 Nov Inventory snapshots cannot be back-filled (Inventory is a point-in-time view).
  - Pick Pack / Order Enquiry / Sales for 19–23 Nov: their 7-day catch-up should refill them on the next runs. Check after the first green run.
  - Branch `diag/location-switch` is diagnostic only; delete it.
- **Related:** 2026-10-19-new-vinculum-login-exports-only-nm4.md, 2026-10-23-sales-17-location-loop-timeouts.md.

## Follow-up 23-Nov: Vinculum Sales still failed after the skip fix
- **Symptom:** run 45158602956, on every date: `SALES_LOCATION=(blank) -> export from A02 only`, then `[!] 19/11/2026 attempt 1/2 failed: VIN_LOCATIONS='A02' leaves no location to export (available: A03, A07, H01, …)`, then `[✗] 4 date(s) not exported`. Sync Sales to SharePoint failed after it (`[✗] 4 date(s) not synced`), because no Sales files existed.
- **Root cause:** `scripts/vinculum_sales_report_to_drive.py` `DEFAULT_SALES_LOCATION = "A02"`. The Sales Report is company-wide (every location returns the same rows), so it is exported from ONE fixed location, and that location was A02, one of the 8 unswitchable ones. The skip fix correctly refused it, which left nothing to export.
- **Fix:** default `H01`, the login's own landing location, which switches fine; the workflow input text is updated too. New test `test_sales_default_location_is_switchable`: the default must never be in `unswitchable_locations()`.
- **Evidence:** `python -m pytest tests -q` gives 169 passed. Live re-run: see the commit / chat.
