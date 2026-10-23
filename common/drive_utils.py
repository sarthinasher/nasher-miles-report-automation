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
import http.client
import socket
import ssl
import time
import random

import httplib2
from google_auth_httplib2 import AuthorizedHttp
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
    string stored in an environment variable (a GitHub Secret in CI).
    Each call makes a NEW connection pool: upload_to_drive rebuilds the client
    after a connection error (a dropped TLS connection stays broken)."""
    raw_json = os.environ.get(service_account_json_env)
    if not raw_json:
        raise RuntimeError(f"Missing environment variable: {service_account_json_env}")

    with open(tmp_key_path, "w") as f:
        f.write(raw_json)

    creds = service_account.Credentials.from_service_account_file(
        tmp_key_path, scopes=DRIVE_SCOPES
    )
    # A socket timeout, so a dead connection fails (and is retried) instead of
    # hanging the job until the workflow timeout.
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=DRIVE_SOCKET_TIMEOUT))
    return build("drive", "v3", http=http, cache_discovery=False)


# ── Retries (2026-10-23) ─────────────────────────────────────
# Uploads failed with "EOF occurred in violation of protocol (_ssl.c:2437)" on the
# FIRST Drive call after 30+ min of browser work (the client is built at the start
# of the run; its idle TLS connection had been dropped). The old code retried only
# HttpError 5xx and gave up on anything else at once. Now the whole upload is
# retried on transient errors with exponential backoff + jitter, on a freshly built
# client. See "Errors and their fixes/2026-10-23-drive-upload-ssl-eof.md".
DRIVE_SOCKET_TIMEOUT = 120                  # seconds per socket operation
DRIVE_MAX_ATTEMPTS = 6                      # whole upload (lookup + create/update)
DRIVE_BACKOFF_BASE = 2.0                    # waits 2, 4, 8, 16, 30 s (+ 0-1 s jitter)
DRIVE_BACKOFF_CAP = 30.0
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024         # resumable chunks; a multiple of 256 KB
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
RETRYABLE_403_REASONS = ("rateLimitExceeded", "userRateLimitExceeded")
_TRANSIENT_TEXT = ("eof occurred in violation of protocol", "connection reset",
                   "connection aborted", "broken pipe", "timed out", "remote end closed")


def _http_status(e):
    try:
        return int(getattr(getattr(e, "resp", None), "status", 0) or 0)
    except (TypeError, ValueError):
        return 0


def is_transient_drive_error(e):
    """True for errors worth retrying: dropped / reset / timed-out connections (SSL
    EOF, ConnectionReset, BrokenPipe, timeouts, http.client and httplib2 transport
    errors) and Drive HttpError 429 / 5xx / 403 rate limits. False for the rest
    (400, 401, 404, a 403 permission error...): those are raised at once."""
    if isinstance(e, HttpError):
        status = _http_status(e)
        if status in RETRYABLE_HTTP_STATUS:
            return True
        if status == 403:
            details = str(getattr(e, "error_details", "") or "")
            content = getattr(e, "content", b"") or b""
            if isinstance(content, bytes):
                content = content.decode("utf-8", "replace")
            return any(r in details or r in content for r in RETRYABLE_403_REASONS)
        return False
    if isinstance(e, (ssl.SSLError, ConnectionError, TimeoutError, socket.timeout,
                      http.client.HTTPException, httplib2.ServerNotFoundError)):
        return True
    return any(t in str(e).lower() for t in _TRANSIENT_TEXT)


def backoff_seconds(attempt, rand=random.random):
    """Wait after failed attempt n (1, 2, ...): 2, 4, 8, 16, 30 s + 0-1 s jitter."""
    return min(DRIVE_BACKOFF_CAP, DRIVE_BACKOFF_BASE * 2 ** (attempt - 1)) + rand()


def _default_rebuild():
    """A fresh client when the service-account secret is set, else None (keep the old)."""
    if not os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON"):
        return None
    return get_drive_service()


def with_drive_retries(fn, drive_service, what, rebuild=_default_rebuild, sleep=time.sleep,
                       max_attempts=DRIVE_MAX_ATTEMPTS, rand=random.random):
    """Returns fn(service). On a transient error: waits (exponential backoff + jitter),
    rebuilds the client (rebuild() -> new service, or None = keep the old one) and
    tries again, max_attempts in all. A non-transient error is raised at once, the
    last transient one after max_attempts."""
    service = drive_service
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(service)
        except Exception as e:
            if not is_transient_drive_error(e) or attempt >= max_attempts:
                raise
            wait = backoff_seconds(attempt, rand)
            print(f"    [!] Drive {what}: {type(e).__name__}: {e} (transient) - attempt "
                  f"{attempt}/{max_attempts} failed, retrying in {wait:.1f}s on a new connection...")
            sleep(wait)
            if rebuild:
                try:
                    service = rebuild() or service
                except Exception as rebuild_error:
                    print(f"    [!] Could not rebuild the Drive client ({rebuild_error!r}) - reusing it.")


def _upload_once(drive_service, local_filepath, drive_filename, folder_id, chunksize):
    """One try: look up a same-named file, then a resumable create / update driven
    with next_chunk(). Returns (file id, "Updated existing" | "Uploaded new")."""
    safe = drive_filename.replace("\\", "\\\\").replace("'", "\\'")
    results = drive_service.files().list(
        q=f"name='{safe}' and '{folder_id}' in parents and trashed=false",
        fields="files(id,name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    existing = results.get("files", [])

    ext = os.path.splitext(local_filepath)[1].lower()
    source_mime_type = MIME_BY_EXT.get(
        ext, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    media = MediaFileUpload(local_filepath, mimetype=source_mime_type, resumable=True,
                            chunksize=chunksize)
    if existing:
        request = drive_service.files().update(
            fileId=existing[0]["id"], media_body=media, fields="id", supportsAllDrives=True)
        how = "Updated existing"
    else:
        file_metadata = {"name": drive_filename, "parents": [folder_id], "mimeType": SHEET_MIME_TYPE}
        request = drive_service.files().create(
            body=file_metadata, media_body=media, fields="id", supportsAllDrives=True)
        how = "Uploaded new"
    response = None
    while response is None:
        _status, response = request.next_chunk()
    return response.get("id"), how


def upload_to_drive(drive_service, local_filepath, drive_filename, folder_id,
                    sleep=time.sleep, rebuild=_default_rebuild,
                    max_attempts=DRIVE_MAX_ATTEMPTS, chunksize=UPLOAD_CHUNK_SIZE):
    """Uploads a file to a Drive folder and converts it into a native Google
    Sheet, overwriting a same-named Sheet if one already exists there.
    Resumable upload, chunksize bytes per request. The whole upload (lookup +
    create/update) is retried on transient errors - SSL EOF, connection reset,
    timeouts, HTTP 429/5xx, 403 rate limits - up to max_attempts times with
    exponential backoff (2-30 s, about 1 min of waiting in all) on a rebuilt
    client. The lookup runs again on each try, so a create that reached Drive
    before the connection broke is updated, not duplicated. Returns the Drive
    file ID, or None on failure (400 / 404 / permission errors: no retry)."""
    try:
        file_id, how = with_drive_retries(
            lambda svc: _upload_once(svc, local_filepath, drive_filename, folder_id, chunksize),
            drive_service, f"upload of {drive_filename}", rebuild=rebuild, sleep=sleep,
            max_attempts=max_attempts)
    except Exception as e:
        print(f"    [!] Drive upload error: {type(e).__name__}: {e}")
        return None
    print(f"    [OK] {how} Google Sheet in Drive: {drive_filename}")
    return file_id


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


def list_file_names(drive_service, folder_id, name_contains, sleep=time.sleep):
    """Names of the non-trashed files in folder_id whose name contains
    name_contains. Returns None if Drive can't be listed (callers then must
    not skip any work)."""
    query = f"'{folder_id}' in parents and trashed=false"
    if name_contains:
        query += f" and name contains '{name_contains}'"

    def list_all(service):
        names, token = set(), None
        while True:
            resp = service.files().list(
                q=query,
                fields="nextPageToken, files(name)", pageSize=1000, pageToken=token,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            names.update(f["name"] for f in resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                return names

    try:
        return with_drive_retries(list_all, drive_service, "folder listing", sleep=sleep)
    except Exception as e:
        print(f"    [!] Could not list Drive folder ({e!r}) — not skipping anything.")
        return None


def find_drive_file_by_name(drive_service, folder_id, name):
    """Metadata {id, name, mimeType, modifiedTime} of the newest non-trashed file in
    folder_id named exactly `name`, or None."""
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    results = drive_service.files().list(
        q=f"'{folder_id}' in parents and trashed=false and name='{safe}'",
        fields="files(id,name,mimeType,modifiedTime)",
        orderBy="modifiedTime desc",
        pageSize=10,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = results.get("files", [])
    return files[0] if files else None


def get_newest_drive_file(drive_service, folder_id):
    """Returns metadata for the most recently modified file in a Drive folder,
    or None if the folder is empty. Result: {id, name, mimeType, modifiedTime}."""
    results = drive_service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name,mimeType,modifiedTime)",
        orderBy="modifiedTime desc",
        pageSize=10,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = results.get("files", [])
    return files[0] if files else None


def download_drive_file_as_xlsx(drive_service, file_meta, dest_path):
    """Downloads a Drive file to dest_path as .xlsx.
    - If it's a native Google Sheet, export it to xlsx.
    - If it's already an xlsx/xls/csv, download the raw bytes.
    Returns dest_path."""
    from googleapiclient.http import MediaIoBaseDownload
    import io

    file_id = file_meta["id"]
    mime = file_meta.get("mimeType", "")

    if mime == "application/vnd.google-apps.spreadsheet":
        request = drive_service.files().export_media(
            fileId=file_id,
            mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    else:
        request = drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()

    with open(dest_path, "wb") as f:
        f.write(buf.getvalue())
    return dest_path
