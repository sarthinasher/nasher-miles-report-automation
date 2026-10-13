"""
Nasher Miles - Drive -> SharePoint sync (Vinculum Sales)
==========================================================
Takes yesterday's (IST) file from the Vinculum Sales Google Drive folder BY NAME
(DD.MM.YYYY = invoice date; REPORT_DATE=DD/MM/YYYY[,..|-..] for other days), exports
it to .xlsx and uploads it into the SharePoint 'Sales/Vinculum' folder as
DD.MM.YYYY.xlsx (replacing a same-named file). A missing file fails the run instead
of uploading an older day under yesterday's name (2026-10-13).

Auth:
  - Google Drive: service account (GDRIVE_SERVICE_ACCOUNT_JSON)  [read side]
  - SharePoint  : Azure AD app-only (SP_CLIENT_ID / SP_TENANT_ID / SP_CLIENT_SECRET)

Run locally for testing:
    GDRIVE_SERVICE_ACCOUNT_JSON='{...}' \
    SP_CLIENT_ID=... SP_TENANT_ID=... SP_CLIENT_SECRET=... \
        python scripts/sync_sales_to_sharepoint.py
"""

import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.drive_utils import (
    get_drive_service,
    find_drive_file_by_name,
    download_drive_file_as_xlsx,
)
from common.report_dates import (
    SALES_REPORT_NAME, ReportDateError, drive_filename, parse_report_dates,
)
from common.sharepoint_utils import (
    get_graph_token,
    resolve_site_id,
    resolve_drive_id,
    ensure_folder_path,
    upload_file_to_sharepoint,
)

# ── SETTINGS ──────────────────────────────────────────────
# Source: Vinculum Sales Drive folder (same one the Sales report writes to)
DRIVE_FOLDER_ID = os.environ.get(
    "VINCULUM_SALES_DRIVE_FOLDER_ID", "1iL94fl0nUOxZ_pEWH_AuxnqBYsYjsjt_"
)

# Destination: SharePoint site + folder path within its document library
SHAREPOINT_SITE_URL = os.environ.get(
    "SP_SITE_URL", "https://nashermilesprivatelimite452.sharepoint.com/sites/NasherMiles"
)
SHAREPOINT_FOLDER_PATH = os.environ.get("SP_SALES_FOLDER", "Sales/Vinculum")

LOCAL_TMP = "/tmp/sp_sync"
IST = timezone(timedelta(hours=5, minutes=30))   # India: no DST


def fail(message, code=1):
    print(f"\n[✗] {message}")
    sys.exit(code)


def validate_env():
    missing = [n for n, v in [
        ("GDRIVE_SERVICE_ACCOUNT_JSON", os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")),
        ("SP_CLIENT_ID", os.environ.get("SP_CLIENT_ID")),
        ("SP_TENANT_ID", os.environ.get("SP_TENANT_ID")),
        ("SP_CLIENT_SECRET", os.environ.get("SP_CLIENT_SECRET")),
    ] if not v]
    if missing:
        fail(f"Missing required environment variables/secrets: {', '.join(missing)}")


def sync_dates(today, spec):
    """Report dates to mirror: REPORT_DATE (one / list / range) or yesterday (IST)."""
    requested = parse_report_dates(spec, today)
    return requested or [today - timedelta(days=1)]


def main():
    validate_env()
    os.makedirs(LOCAL_TMP, exist_ok=True)

    print("=" * 55)
    print("  Nasher Miles - Drive -> SharePoint (Vinculum Sales)")
    print("=" * 55)

    today = datetime.now(IST).date()
    try:
        days = sync_dates(today, os.environ.get("REPORT_DATE", ""))
    except ReportDateError as e:
        fail(f"REPORT_DATE: {e}", code=2)

    print("\n[1] Authenticating Google Drive (service account)...")
    drive_service = get_drive_service()
    print("    [OK] Drive ready.")

    # 2026-10-13: the file is picked by its NAME (= invoice date), never "the newest
    # file". With the Vinculum export failing, the newest file was 10.10.2026 and it
    # was uploaded to SharePoint as 11.10.2026.xlsx (10-Oct invoices under 11-Oct).
    print(f"\n[2] Finding {', '.join(drive_filename(d, *SALES_REPORT_NAME) for d in days)} "
          f"in the Vinculum Sales Drive folder...")
    found, missing = [], []
    for day in days:
        meta = find_drive_file_by_name(drive_service, DRIVE_FOLDER_ID,
                                       drive_filename(day, *SALES_REPORT_NAME))
        (found if meta else missing).append((day, meta))
    for day, _ in missing:
        print(f"    [!] {drive_filename(day, *SALES_REPORT_NAME)} is not in Drive yet "
              f"(Vinculum Sales Report export missing) — not synced.")
    if not found:
        fail("Nothing to sync — no file for the requested date(s).")

    print("\n[3] Authenticating SharePoint (Microsoft Graph app-only)...")
    token = get_graph_token()
    site_id = resolve_site_id(token, SHAREPOINT_SITE_URL)
    drive_id = resolve_drive_id(token, site_id)
    print("    [OK] SharePoint site + document library resolved.")
    print(f"\n[4] Ensuring folder path exists: {SHAREPOINT_FOLDER_PATH}")
    ensure_folder_path(token, drive_id, SHAREPOINT_FOLDER_PATH)

    for day, meta in found:
        xlsx_name = drive_filename(day, *SALES_REPORT_NAME) + ".xlsx"
        local_path = os.path.join(LOCAL_TMP, xlsx_name)
        print(f"\n[5] {meta['name']} (modified {meta.get('modifiedTime')}) -> {xlsx_name}")
        download_drive_file_as_xlsx(drive_service, meta, local_path)
        print(f"    [OK] Local file ready: {local_path} ({os.path.getsize(local_path)} bytes)")
        web_url = upload_file_to_sharepoint(
            token, drive_id, local_path, SHAREPOINT_FOLDER_PATH, filename=xlsx_name
        )
        print(f"    [OK] Uploaded (replaces a same-named file). webUrl: {web_url}")
        try:
            os.remove(local_path)
        except Exception as e:
            print(f"    [!] Could not delete local temp file: {e}")

    print("\n" + "=" * 55)
    print(f"  Synced: {', '.join(meta['name'] for _, meta in found)}")
    print(f"  Site  : {SHAREPOINT_SITE_URL}")
    print(f"  Folder: {SHAREPOINT_FOLDER_PATH}")
    print("=" * 55)
    if missing:
        fail(f"{len(missing)} date(s) not synced (file missing in Drive): "
             f"{', '.join(drive_filename(d, *SALES_REPORT_NAME) for d, _ in missing)}")


if __name__ == "__main__":
    main()
