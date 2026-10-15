# Punched GT orders (e.g. Arvind) still show "OMS Punch" pending on the Full Flow dashboard, no Order No

- **Date:** 2026-10-15
- **Reported as:** "is the FULL FLOW FMS BROKEN THE ORDERS PUNCHED SHOULD BE THERE WITH THEIR ORDER ID SO WWE AVOID DUPLICATE PUCHING OF ORDERS WHY ISNT IT THERE LIKE FOR EXAMPLE ARVIND ORDERS WERE PUNCHED BUT still they are showing up on the dashboard with the option to punch it"
- **Symptom:** GTs punched in Vinculum since 11-Oct have blank Order No (P) / stage-1 Actual (S) in GT Full Flow FMS 2026-2027, so the FMS_Dashboard (GT_FMS/dashboard.js) lists them under OMS Punch -> risk of punching the same GT twice.
- **Where:**
  - GT_FMS/dashboard.js:836 `punch: !!ffToDate_(r[C_S1_ACTUAL])` (column S blank -> "OMS Punch" pending).
  - nasher-fms-automation fills P/Q/S from the daily `OrderEnquiry_DD-MM-YYYY` files (see nasher-fms-automation record 2026-10-10-order-no-blank-until-invoiced.md).
  - This repo, workflow "Order Enquiry Report to Drive" produces those files; it fails at the Vinculum login.
- **Root cause:** the Vinculum login used by all report jobs (secrets VIN_LOGIN_ID / VIN_PASSWORD, last set 2026-09-21) has been rejected since 12-Oct, so no OrderEnquiry file after 11-Oct exists and the nightly FMS job has nothing to stamp Order No / punch date from.
- **Why it wasn't caught:** the job failures were visible only as red runs in GitHub Actions; nothing alerts ops, and the FMS job itself reports success because it runs fine on the stale files.
- **Fix:** none possible in code. The credentials must be fixed by a human: log in to https://nashermiles.vineretail.com by hand as the VIN_LOGIN_ID user (or a new service user if that account was the handed-over user's and is disabled), then update the two repo secrets and re-run "Order Enquiry Report to Drive" (it back-fills the last 7 days), then "GT FMS Automation" in nasher-fms-automation.
- **Evidence:** `gh run view <latest Order Enquiry run> --log-failed` (2026-10-15T09:18Z):
  `[✗] Vinculum login failed for user '***': no message shown, still on the login page -> check VIN_LOGIN_ID / VIN_PASSWORD ... [page after submit: https://nashermiles.vineretail.com/eRetailWeb/doLogin]`,
  `OrderEnquiry_11-10-2026: FAILED`, `12-10 / 13-10 / 14-10: FAILED: not run (Vinculum login failed)`.
  Last successful Order Enquiry run: 2026-10-11T15:55:17Z. Every Vinculum job (Order Enquiry, Pick Pack, Sales, Inventory, SharePoint sync) failed on 14-Oct and 15-Oct.
- **Blast radius:** Order No / Team / Order Date (P/Q/S), WH-process and invoice columns, inventory-based checks on the GT form ("No warehouse data available"), Sales Master, SharePoint sales sync — every Vinculum-fed column is frozen at 10/11-Oct.
- **Still open:** the credential fix (needs the user). Until then ops must check Vinculum before punching any GT that the dashboard shows as not punched.
- **Related:** 2026-10-13-vinculum-login-failing-and-no-catch-up.md (same login failure, first seen 12-Oct ~16:20 IST).
