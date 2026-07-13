"""
drive_to_sharepoint_inventory.py
Pulls daily inventory Google Sheets from a Google Drive folder,
exports each to XLSX, and uploads to SharePoint → Inventory/Daily Inventory.

Reuses the established patterns:
  - Google Drive  : Cloud service account (google-api-python-client)
  - SharePoint    : Microsoft Graph API, client-credentials app registration
"""

import io
import os
import re
import requests

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ── CONFIG ────────────────────────────────────────────────────────────────────
# Google Drive
DRIVE_FOLDER_ID  = "1BSVYTIkVTQ72q5p1wkl9ld_eOVEjAfIc"
# Path to the service-account JSON key (same SA used elsewhere).
# In GitHub Actions this is written from a secret; locally point at your key file.
GDRIVE_SA_JSON   = os.environ.get("GDRIVE_SA_JSON", "service_account.json")
DRIVE_SCOPES     = ["https://www.googleapis.com/auth/drive.readonly"]

# SharePoint (Microsoft Graph)
TENANT_ID        = os.environ["SP_TENANT_ID"]
CLIENT_ID        = os.environ["SP_CLIENT_ID"]
CLIENT_SECRET    = os.environ["SP_CLIENT_SECRET"]

SITE_NAME        = "NasherMiles"
HOSTNAME         = "nashermilesprivatelimite452.sharepoint.com"
TARGET_FOLDER    = "Inventory/Daily Inventory"

# If True, upload as Inventory_YYYYMMDD.xlsx (what the singles converter expects).
# If False, keep the original Drive name.
RENAME_FOR_PIPELINE = True

# Skip a date if a matching file already exists in the SharePoint target folder.
SKIP_IF_EXISTS      = True

XLSX_MIME       = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GSHEET_MIME     = "application/vnd.google-apps.spreadsheet"
# ─────────────────────────────────────────────────────────────────────────────


# ── Google Drive helpers ──────────────────────────────────────────────────────
def get_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        GDRIVE_SA_JSON, scopes=DRIVE_SCOPES
    )
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    print("  Google Drive service ready ✓")
    return service


def list_drive_files(service, folder_id):
    """List all non-trashed files in the folder (handles pagination)."""
    files = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed = false"
    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            pageSize=1000,
            pageToken=page_token,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def download_drive_file(service, file_obj):
    """
    Return XLSX bytes for a Drive file.
    - Native Google Sheet  → export as XLSX
    - Already an xlsx/xls  → download as-is
    """
    file_id   = file_obj["id"]
    mime      = file_obj["mimeType"]

    if mime == GSHEET_MIME:
        request = service.files().export_media(fileId=file_id, mimeType=XLSX_MIME)
    else:
        request = service.files().get_media(fileId=file_id)

    buffer     = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    buffer.seek(0)
    return buffer.read()


# ── Graph API helpers (same as inventory_sets_to_singles.py) ──────────────────
def get_access_token():
    url  = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        "grant_type":    "client_credentials",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope":         "https://graph.microsoft.com/.default",
    }
    r = requests.post(url, data=data)
    r.raise_for_status()
    print("  Access token obtained ✓")
    return r.json()["access_token"]


def get_headers(token):
    return {"Authorization": f"Bearer {token}"}


def get_site_id(token):
    url = f"https://graph.microsoft.com/v1.0/sites/{HOSTNAME}:/sites/{SITE_NAME}"
    r   = requests.get(url, headers=get_headers(token))
    r.raise_for_status()
    print("  Site ID obtained ✓")
    return r.json()["id"]


def get_drive_id(token, site_id):
    url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives"
    r   = requests.get(url, headers=get_headers(token))
    r.raise_for_status()
    print("  Drive ID obtained ✓")
    return r.json()["value"][0]["id"]


def list_sharepoint_files(token, drive_id, folder_path):
    url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{folder_path}:/children"
    r   = requests.get(url, headers=get_headers(token))
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return r.json().get("value", [])


def upload_file(token, drive_id, folder_path, filename, content):
    url = (f"https://graph.microsoft.com/v1.0/drives/{drive_id}"
           f"/root:/{folder_path}/{filename}:/content")
    headers = get_headers(token)
    headers["Content-Type"] = "application/octet-stream"
    r = requests.put(url, headers=headers, data=content)
    r.raise_for_status()
    print(f"  Uploaded → {folder_path}/{filename} ✓")


# ── Filename handling ─────────────────────────────────────────────────────────
def parse_date_ddmmyyyy(name):
    """
    Drive names look like '08-07-2026_12-36' (DD-MM-YYYY_HH-MM).
    Return 'YYYYMMDD' or None.
    """
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})", name.strip())
    if not m:
        return None
    day, month, year = m.groups()
    return f"{year}{month}{day}"


def target_filename(original_name, date_str):
    if RENAME_FOR_PIPELINE and date_str:
        return f"Inventory_{date_str}.xlsx"
    base = original_name
    if not base.lower().endswith((".xlsx", ".xls")):
        base += ".xlsx"
    return base


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  Google Drive → SharePoint  |  Daily Inventory Sync")
    print("=" * 60)

    # Google Drive
    print("\nConnecting to Google Drive...")
    drive_service = get_drive_service()
    drive_files   = list_drive_files(drive_service, DRIVE_FOLDER_ID)
    print(f"  Found {len(drive_files)} file(s) in Drive folder")

    # SharePoint
    print("\nConnecting to SharePoint...")
    token    = get_access_token()
    site_id  = get_site_id(token)
    drive_id = get_drive_id(token, site_id)

    existing = set()
    if SKIP_IF_EXISTS:
        for f in list_sharepoint_files(token, drive_id, TARGET_FOLDER):
            existing.add(f["name"].lower())
        print(f"  {len(existing)} file(s) already in target folder")

    success = skipped = failed = 0

    for f in drive_files:
        name     = f["name"]
        date_str = parse_date_ddmmyyyy(name)

        if date_str is None:
            print(f"\nSkipping (unrecognised name): {name}")
            skipped += 1
            continue

        out_name = target_filename(name, date_str)

        if SKIP_IF_EXISTS and out_name.lower() in existing:
            print(f"\nAlready in SharePoint, skipping: {out_name}")
            skipped += 1
            continue

        try:
            print(f"\nProcessing: {name}  →  {out_name}")
            content = download_drive_file(drive_service, f)
            print(f"  Downloaded {len(content):,} bytes")
            upload_file(token, drive_id, TARGET_FOLDER, out_name, content)
            success += 1
        except Exception as e:
            print(f"  ERROR on {name}: {e}")
            failed += 1

    print("\n" + "=" * 60)
    print(f"  Done! ✓  {success} uploaded | {skipped} skipped | {failed} failed")
    print(f"  Target: {TARGET_FOLDER}")
    print("=" * 60)


if __name__ == "__main__":
    main()
