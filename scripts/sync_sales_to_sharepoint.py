"""
Nasher Miles - Drive -> SharePoint sync (Vinculum Sales)
==========================================================
Grabs the NEWEST file from the Vinculum Sales Google Drive folder, exports it
to .xlsx, and uploads it into the SharePoint 'Sales/Vinculum' folder,
keeping its date-based name (e.g. 08.07.2026.xlsx).

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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.drive_utils import (
    get_drive_service,
    get_newest_drive_file,
    download_drive_file_as_xlsx,
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
IST = ZoneInfo("Asia/Kolkata")


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


def main():
    validate_env()
    os.makedirs(LOCAL_TMP, exist_ok=True)

    print("=" * 55)
    print("  Nasher Miles - Drive -> SharePoint (Vinculum Sales)")
    print("=" * 55)

    # ── 1. Find newest file in the Drive folder ──────────
    print("\n[1] Authenticating Google Drive (service account)...")
    drive_service = get_drive_service()
    print("    [OK] Drive ready.")

    print("\n[2] Finding newest file in the Vinculum Sales Drive folder...")
    newest = get_newest_drive_file(drive_service, DRIVE_FOLDER_ID)
    if not newest:
        fail("Drive folder is empty — nothing to sync.")
    print(f"    [OK] Newest file: {newest['name']} (modified {newest.get('modifiedTime')})")

    # ── 2. Export/download it locally as .xlsx ───────────
    # Name the SharePoint file from YESTERDAY's date in DD.MM.YYYY format,
    # so it's always e.g. 08.07.2026.xlsx regardless of how the source Drive
    # file happens to be named.
    yesterday = datetime.now(IST) - timedelta(days=1)
    xlsx_name = yesterday.strftime("%d.%m.%Y") + ".xlsx"
    local_path = os.path.join(LOCAL_TMP, xlsx_name)

    print(f"\n[3] Downloading/exporting to {xlsx_name}...")
    download_drive_file_as_xlsx(drive_service, newest, local_path)
    size = os.path.getsize(local_path)
    print(f"    [OK] Local file ready: {local_path} ({size} bytes)")

    # ── 3. Upload to SharePoint ──────────────────────────
    print("\n[4] Authenticating SharePoint (Microsoft Graph app-only)...")
    token = get_graph_token()
    site_id = resolve_site_id(token, SHAREPOINT_SITE_URL)
    drive_id = resolve_drive_id(token, site_id)
    print("    [OK] SharePoint site + document library resolved.")

    print(f"\n[5] Ensuring folder path exists: {SHAREPOINT_FOLDER_PATH}")
    ensure_folder_path(token, drive_id, SHAREPOINT_FOLDER_PATH)

    print(f"\n[6] Uploading {xlsx_name} to SharePoint {SHAREPOINT_FOLDER_PATH}/ ...")
    web_url = upload_file_to_sharepoint(
        token, drive_id, local_path, SHAREPOINT_FOLDER_PATH, filename=xlsx_name
    )
    print(f"    [OK] Uploaded. webUrl: {web_url}")

    # ── 4. Clean up local temp ───────────────────────────
    try:
        os.remove(local_path)
        print(f"    [✓] Local temp file deleted: {local_path}")
    except Exception as e:
        print(f"    [!] Could not delete local temp file: {e}")

    print("\n" + "=" * 55)
    print("  [✓] SUCCESS! Newest Sales file mirrored to SharePoint.")
    print(f"  Site  : {SHAREPOINT_SITE_URL}")
    print(f"  Folder: {SHAREPOINT_FOLDER_PATH}")
    print(f"  File  : {xlsx_name}")
    print("=" * 55)


if __name__ == "__main__":
    main()
