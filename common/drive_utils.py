"""
Shared Google Drive helpers used by every report script in this repo.

All scripts authenticate the same way (service account via
GDRIVE_SERVICE_ACCOUNT_JSON env var / GitHub Secret) and upload the same
way (convert the downloaded file into a native Google Sheet). Keeping this
in one place means adding a new report script is just: write the scraping
logic, then call these two functions.
"""

import os
import glob
import time
import random
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from google.oauth2 import service_account

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

MIME_BY_EXT = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls":  "application/vnd.ms-excel",
    ".csv":  "text/csv",
}
SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"


def get_drive_service(service_account_json_env="GDRIVE_SERVICE_ACCOUNT_JSON",
                       tmp_key_path="/tmp/service_account.json"):
    """Builds an authenticated Drive API client from a service account JSON
    string stored in an environment variable (a GitHub Secret in CI)."""
    raw_json = os.environ.get(service_account_json_env)
    if not raw_json:
        raise RuntimeError(f"Missing environment variable: {service_account_json_env}")

    with open(tmp_key_path, "w") as f:
        f.write(raw_json)

    creds = service_account.Credentials.from_service_account_file(
        tmp_key_path, scopes=DRIVE_SCOPES
    )
    return build("drive", "v3", credentials=creds)


def upload_to_drive(drive_service, local_filepath, drive_filename, folder_id):
    """Uploads a file to a Drive folder and converts it into a native Google
    Sheet, overwriting a same-named Sheet if one already exists there.
    Retries transient Google 5xx errors (e.g. 'Internal Error') up to 3
    times with backoff. Returns the Drive file ID, or None on failure."""
    for attempt in range(3):
        try:
            results = drive_service.files().list(
                q=f"name='{drive_filename}' and '{folder_id}' in parents and trashed=false",
                fields="files(id,name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            ).execute()
            existing = results.get("files", [])

            ext = os.path.splitext(local_filepath)[1].lower()
            source_mime_type = MIME_BY_EXT.get(
                ext, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            media = MediaFileUpload(local_filepath, mimetype=source_mime_type, resumable=True)
            file_metadata = {"name": drive_filename, "parents": [folder_id], "mimeType": SHEET_MIME_TYPE}

            if existing:
                file_id = existing[0]["id"]
                updated = drive_service.files().update(
                    fileId=file_id,
                    media_body=media,
                    supportsAllDrives=True
                ).execute()
                print(f"    [OK] Updated existing Google Sheet in Drive: {drive_filename}")
                return updated.get("id")
            else:
                created = drive_service.files().create(
                    body=file_metadata,
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True
                ).execute()
                print(f"    [OK] Uploaded new Google Sheet to Drive: {drive_filename}")
                return created.get("id")

        except HttpError as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            if status and 500 <= int(status) < 600 and attempt < 2:
                wait = 2 ** attempt + random.random()
                print(f"    [!] Drive {status} (transient) — retrying in {wait:.1f}s...")
                time.sleep(wait)
                continue
            print(f"    [!] Drive upload error: {e}")
            return None
        except Exception as e:
            print(f"    [!] Drive upload error: {e}")
            return None
    return None


def get_latest_downloaded_file(folder, extensions=(".xlsx", ".xls", ".csv"), since_ts=None):
    """Finds the most recently modified file matching the given extensions,
    optionally only considering files created/modified after since_ts."""
    candidates = []
    for ext in extensions:
        candidates.extend(glob.glob(os.path.join(folder, f"*{ext}")))
    if since_ts is not None:
        candidates = [f for f in candidates if os.path.getmtime(f) >= since_ts]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)
