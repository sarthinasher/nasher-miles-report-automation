"""
Shared SharePoint (Microsoft Graph) helpers.

App-only authentication via an Azure AD app registration:
  - client id, tenant id come from env / GitHub Secrets
  - client secret comes from a GitHub Secret (never hardcoded)
  - permission: Sites.ReadWrite.All (application), admin-consented

Provides:
  - get_graph_token()          -> bearer access token string
  - resolve_site_id()          -> Graph site id for a SharePoint site URL
  - resolve_drive_id()         -> the site's default document library drive id
  - ensure_folder_path()       -> make sure a nested folder path exists
  - upload_file_to_sharepoint()-> upload/replace a file into a folder path
"""

import os
import requests
from urllib.parse import urlparse

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


def get_graph_token(client_id_env="SP_CLIENT_ID",
                     tenant_id_env="SP_TENANT_ID",
                     client_secret_env="SP_CLIENT_SECRET"):
    """Fetch an app-only OAuth2 token from Microsoft identity platform."""
    client_id = os.environ.get(client_id_env)
    tenant_id = os.environ.get(tenant_id_env)
    client_secret = os.environ.get(client_secret_env)
    missing = [n for n, v in [
        (client_id_env, client_id),
        (tenant_id_env, tenant_id),
        (client_secret_env, client_secret),
    ] if not v]
    if missing:
        raise RuntimeError(f"Missing SharePoint env vars: {', '.join(missing)}")

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    resp = requests.post(url, data=data, timeout=30)
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in token response: {resp.text[:300]}")
    return token


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def resolve_site_id(token, site_url):
    """Turn a SharePoint site URL into a Graph site id.
    e.g. https://contoso.sharepoint.com/sites/NasherMiles
    """
    parsed = urlparse(site_url)
    hostname = parsed.netloc                      # contoso.sharepoint.com
    site_path = parsed.path.rstrip("/")           # /sites/NasherMiles
    url = f"{GRAPH_ROOT}/sites/{hostname}:{site_path}"
    resp = requests.get(url, headers=_headers(token), timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def resolve_drive_id(token, site_id):
    """Get the site's default document library (drive) id."""
    url = f"{GRAPH_ROOT}/sites/{site_id}/drive"
    resp = requests.get(url, headers=_headers(token), timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def ensure_folder_path(token, drive_id, folder_path):
    """Ensure each segment of folder_path (e.g. 'Sales/Vinculum') exists,
    creating any missing folders. Safe to call when they already exist."""
    segments = [s for s in folder_path.strip("/").split("/") if s]
    parent = "root"
    built = ""
    for seg in segments:
        built = f"{built}/{seg}" if built else seg
        # Try to fetch this folder; if it 404s, create it under its parent.
        check_url = f"{GRAPH_ROOT}/drives/{drive_id}/root:/{built}"
        r = requests.get(check_url, headers=_headers(token), timeout=30)
        if r.status_code == 200:
            parent = built
            continue
        # Create under the current parent
        if parent == "root":
            create_url = f"{GRAPH_ROOT}/drives/{drive_id}/root/children"
        else:
            create_url = f"{GRAPH_ROOT}/drives/{drive_id}/root:/{parent}:/children"
        body = {
            "name": seg,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "replace",
        }
        cr = requests.post(create_url, headers=_headers(token), json=body, timeout=30)
        cr.raise_for_status()
        parent = built
    return folder_path.strip("/")


def upload_file_to_sharepoint(token, drive_id, local_filepath, folder_path, filename=None):
    """Upload (or overwrite) a file into folder_path on the drive.
    Uses a simple PUT for files up to ~4MB, and a resumable upload session
    for larger files. Returns the created/updated item's webUrl."""
    filename = filename or os.path.basename(local_filepath)
    dest = f"{folder_path.strip('/')}/{filename}".strip("/")
    size = os.path.getsize(local_filepath)

    if size <= 4 * 1024 * 1024:
        # Simple upload
        url = f"{GRAPH_ROOT}/drives/{drive_id}/root:/{dest}:/content"
        with open(local_filepath, "rb") as f:
            data = f.read()
        headers = _headers(token)
        headers["Content-Type"] = "application/octet-stream"
        resp = requests.put(url, headers=headers, data=data, timeout=120)
        resp.raise_for_status()
        return resp.json().get("webUrl")

    # Large file: upload session (chunked)
    session_url = f"{GRAPH_ROOT}/drives/{drive_id}/root:/{dest}:/createUploadSession"
    body = {"item": {"@microsoft.graph.conflictBehavior": "replace"}}
    s = requests.post(session_url, headers=_headers(token), json=body, timeout=30)
    s.raise_for_status()
    upload_url = s.json()["uploadUrl"]

    chunk_size = 5 * 1024 * 1024  # 5 MB (must be a multiple of 320 KiB)
    with open(local_filepath, "rb") as f:
        start = 0
        while start < size:
            chunk = f.read(chunk_size)
            end = start + len(chunk) - 1
            headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end}/{size}",
            }
            r = requests.put(upload_url, headers=headers, data=chunk, timeout=300)
            # 202 = accepted (more chunks); 200/201 = finished
            if r.status_code not in (200, 201, 202):
                r.raise_for_status()
            start = end + 1
    # Final response (200/201) carries the item metadata
    try:
        return r.json().get("webUrl")
    except Exception:
        return None
