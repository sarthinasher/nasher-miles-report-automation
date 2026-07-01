# Nasher Miles Report Automation

Runs daily report downloads from Vinculum/eRetail and Flipkart, and uploads
each one to a specific Google Drive folder as a native Google Sheet — fully
unattended via GitHub Actions, no local PC required.

## Structure

```
common/
  drive_utils.py          <- shared Drive auth + upload logic (used by every script)
scripts/
  pickpack_report_to_drive.py         <- live
  vinculum_sales_report_to_drive.py   <- placeholder, not wired up yet
.github/workflows/
  pickpack-report.yml                 <- runs daily at 08:30 IST
  vinculum-sales-report.yml           <- template, currently disabled
requirements.txt
```

## Secrets required (Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `VIN_LOGIN_ID` | Vinculum/eRetail login username |
| `VIN_PASSWORD` | Vinculum/eRetail password |
| `GDRIVE_SERVICE_ACCOUNT_JSON` | Full contents of the service account JSON key |

The service account's `client_email` must be shared as **Editor** on every
target Drive folder used by any script in this repo.

## Adding a new report

1. Copy `scripts/pickpack_report_to_drive.py` as a starting template.
2. Swap out the scraping steps (login stays the same; the report
   selection / date / export / download-wait logic will differ per report).
3. Keep using `common.drive_utils.get_drive_service()`,
   `upload_to_drive()`, and `get_latest_downloaded_file()` — don't
   duplicate that logic.
4. Copy `.github/workflows/vinculum-sales-report.yml` as a template for
   the new workflow file, point it at the new script, and pick a `cron`
   time that doesn't overlap with existing runs.
5. If the new report needs its own Drive folder ID, either hardcode it in
   the script (folder IDs aren't secret) or add it as an optional env var
   with a default, same as `PICKPACK_DRIVE_FOLDER_ID`.
6. Test with **Actions → [workflow name] → Run workflow** (manual trigger)
   before enabling the schedule.

## Local testing

```bash
pip install -r requirements.txt
VIN_LOGIN_ID=Sonali VIN_PASSWORD=your_password \
  GDRIVE_SERVICE_ACCOUNT_JSON="$(cat /path/to/service_account.json)" \
  python scripts/pickpack_report_to_drive.py
```
