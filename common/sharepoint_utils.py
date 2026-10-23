"""
Shared SharePoint (Microsoft Graph) helpers.

App-only authentication via an Azure AD app registration:
  - client id, tenant id come from env / GitHub Secrets
  - client secret comes from a GitHub Secret (never hardcoded, never logged)
  - permission: Sites.ReadWrite.All (application), admin-consented

Provides:
  - GraphAuth                  -> app-only token that refreshes itself (expiry / a 401)
  - get_graph_token()          -> one bearer access token string
  - graph_request()            -> one Graph call with retries (429 / 5xx / dropped connection)
  - resolve_site_id()          -> Graph site id for a SharePoint site URL
  - resolve_drive_id()         -> the site's default document library drive id
  - ensure_folder_path()       -> make sure a nested folder path exists (never replaces one)
  - list_folder_files()        -> {name: {size, lastModifiedDateTime}} of a folder
  - upload_file_to_sharepoint()-> upload/replace a file into a folder path

Every function that takes `token` accepts a GraphAuth (preferred: a 401 or an expired
token is refreshed and the call repeated) or a plain token string.

2026-10-23 (see "Errors and their fixes/2026-10-23-sharepoint-sales-sync-no-catch-up.md"):
  - every call is retried on 429 / 500 / 502 / 503 / 504 and on dropped / timed-out
    connections (exponential backoff + jitter, Retry-After honoured); before, one hiccup
    failed the run;
  - a failed token request shows Azure AD's error (AADSTS code + hint), not a bare
    "400 Client Error";
  - ensure_folder_path creates a folder only after a 404 and with conflictBehavior
    "fail" (was: after ANY non-200, with "replace" - a transient 5xx on the existing
    Sales/Vinculum folder could have replaced the whole folder);
  - an upload is checked: SharePoint must report a non-empty file.
"""

import os
import random
import re
import ssl
import time
from urllib.parse import quote, urlparse

import requests

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
LOGIN_ROOT = "https://login.microsoftonline.com"

MAX_ATTEMPTS = 5                       # per Graph call
BACKOFF_BASE = 2.0                     # waits 2, 4, 8, 16 s (+ 0-1 s jitter)
BACKOFF_CAP = 60.0                     # also caps a Retry-After header
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
TOKEN_REFRESH_MARGIN = 300             # renew a token 5 min before it expires
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024  # Graph: simple PUT up to 4 MB
UPLOAD_CHUNK_SIZE = 5 * 1024 * 1024    # upload session chunks: multiple of 320 KiB

# Azure AD errors seen with app-only auth -> what to do about them.
AADSTS_HINTS = {
    "7000222": "the client secret has EXPIRED - create a new one in Azure AD and update "
               "the SP_CLIENT_SECRET secret",
    "7000215": "the client secret is wrong - check SP_CLIENT_SECRET (the secret VALUE, "
               "not its ID)",
    "700016": "no app with this client id in the tenant - check SP_CLIENT_ID / SP_TENANT_ID",
    "90002": "tenant not found - check SP_TENANT_ID",
    "900023": "tenant id is malformed - check SP_TENANT_ID",
}

# Test seams: tests replace these to run without the network or real waits.
_send = requests.request
_sleep = time.sleep
_now = time.time
_rand = random.random


class SharePointError(RuntimeError):
    """A Graph call failed for good (after retries, or a non-retryable status)."""


class SharePointAuthError(SharePointError):
    """The app-only token could not be obtained (bad / expired secret, wrong ids...)."""


# ── retries ──────────────────────────────────────────────────
def backoff_seconds(attempt, retry_after=None):
    """Wait after failed attempt n (1, 2, ...): Retry-After if the server sent one,
    else 2, 4, 8, 16 s; capped at BACKOFF_CAP, + 0-1 s jitter."""
    try:
        base = float(retry_after) if retry_after not in (None, "") else None
    except (TypeError, ValueError):
        base = None
    if base is None:
        base = BACKOFF_BASE * 2 ** (attempt - 1)
    return min(BACKOFF_CAP, max(0.0, base)) + _rand()


def is_transient_exception(e):
    """Dropped / reset / timed-out connections are worth another try."""
    transient = (ConnectionError, TimeoutError, ssl.SSLError)
    exc = getattr(requests, "exceptions", None)
    if exc is not None:
        transient += tuple(getattr(exc, n) for n in
                           ("ConnectionError", "Timeout", "ChunkedEncodingError")
                           if hasattr(exc, n))
    return isinstance(e, transient)


# ── auth ─────────────────────────────────────────────────────
def _env_credentials(client_id_env, tenant_id_env, client_secret_env):
    values = {n: os.environ.get(n) for n in (client_id_env, tenant_id_env, client_secret_env)}
    missing = [n for n, v in values.items() if not v]
    if missing:
        raise SharePointAuthError(f"Missing SharePoint env vars: {', '.join(missing)}")
    return values[client_id_env], values[tenant_id_env], values[client_secret_env]


def describe_token_error(resp):
    """Azure AD's own reason for a refused token request, e.g.
    'invalid_client AADSTS7000222: ... (the client secret has EXPIRED - ...)'."""
    try:
        body = resp.json()
    except Exception:
        return f"HTTP {resp.status_code}: {(resp.text or '')[:300]}"
    error = body.get("error", "")
    desc = (body.get("error_description") or "").splitlines()
    text = f"HTTP {resp.status_code} {error}: {desc[0] if desc else ''}".strip()
    codes = [str(c) for c in body.get("error_codes") or []]
    hints = [AADSTS_HINTS[c] for c in codes if c in AADSTS_HINTS]
    return text + (f" ({'; '.join(hints)})" if hints else "")


def fetch_token(client_id_env="SP_CLIENT_ID", tenant_id_env="SP_TENANT_ID",
                client_secret_env="SP_CLIENT_SECRET", max_attempts=MAX_ATTEMPTS):
    """(access_token, expires_at epoch seconds) from the Microsoft identity platform
    (client credentials). Retries 429 / 5xx / dropped connections; a refused request
    (bad secret, wrong ids) raises SharePointAuthError at once with Azure AD's reason."""
    client_id, tenant_id, client_secret = _env_credentials(
        client_id_env, tenant_id_env, client_secret_env)
    url = f"{LOGIN_ROOT}/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    for attempt in range(1, max_attempts + 1):
        try:
            resp = _send("POST", url, data=data, timeout=30)
        except Exception as e:
            if not is_transient_exception(e) or attempt >= max_attempts:
                raise SharePointAuthError(f"Token request failed: {type(e).__name__}: {e}") from e
            wait = backoff_seconds(attempt)
            print(f"    [!] Token request: {type(e).__name__}: {e} - attempt {attempt}/"
                  f"{max_attempts}, retrying in {wait:.1f}s...")
            _sleep(wait)
            continue
        if resp.status_code in RETRYABLE_STATUS and attempt < max_attempts:
            wait = backoff_seconds(attempt, resp.headers.get("Retry-After"))
            print(f"    [!] Token request: HTTP {resp.status_code} - attempt {attempt}/"
                  f"{max_attempts}, retrying in {wait:.1f}s...")
            _sleep(wait)
            continue
        if resp.status_code != 200:
            raise SharePointAuthError(f"Azure AD refused the token request: "
                                      f"{describe_token_error(resp)}")
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise SharePointAuthError(f"No access_token in the token response "
                                      f"(keys: {sorted(body)})")
        try:
            lifetime = int(body.get("expires_in", 3599))
        except (TypeError, ValueError):
            lifetime = 3599
        return token, _now() + lifetime
    raise SharePointAuthError("Token request failed")  # pragma: no cover - loop always returns


def get_graph_token(client_id_env="SP_CLIENT_ID",
                    tenant_id_env="SP_TENANT_ID",
                    client_secret_env="SP_CLIENT_SECRET"):
    """One app-only OAuth2 token string (no refresh). Prefer GraphAuth."""
    return fetch_token(client_id_env, tenant_id_env, client_secret_env)[0]


class GraphAuth:
    """App-only token that renews itself shortly before it expires and after a 401."""

    def __init__(self, client_id_env="SP_CLIENT_ID", tenant_id_env="SP_TENANT_ID",
                 client_secret_env="SP_CLIENT_SECRET"):
        self._envs = (client_id_env, tenant_id_env, client_secret_env)
        self._token = None
        self._expires_at = 0.0

    def token(self):
        if not self._token or _now() >= self._expires_at - TOKEN_REFRESH_MARGIN:
            self._token, self._expires_at = fetch_token(*self._envs)
        return self._token

    def invalidate(self):
        self._token = None


def _auth_header(token):
    if token is None:
        return {}
    value = token.token() if isinstance(token, GraphAuth) else token
    return {"Authorization": f"Bearer {value}"}


# ── one Graph call ───────────────────────────────────────────
def graph_request(method, url, token, headers=None, max_attempts=MAX_ATTEMPTS,
                  timeout=60, **kwargs):
    """requests.request with retries. Returns the final Response (the caller checks
    the status: a 404 can be an answer). Retried: dropped / timed-out connections and
    429 / 500 / 502 / 503 / 504 (Retry-After honoured). A 401 with a GraphAuth renews
    the token once and repeats the call. token=None sends no Authorization header
    (upload-session URLs are pre-authenticated)."""
    refreshed = False
    attempt = 0
    while True:
        attempt += 1
        h = dict(headers or {})
        h.update(_auth_header(token))
        try:
            resp = _send(method, url, headers=h, timeout=timeout, **kwargs)
        except Exception as e:
            if not is_transient_exception(e):
                raise
            if attempt >= max_attempts:
                raise SharePointError(f"Graph {method} {_short(url)} failed after {attempt} "
                                      f"attempts: {type(e).__name__}: {_safe(e)}") from None
            wait = backoff_seconds(attempt)
            print(f"    [!] Graph {method} {_short(url)}: {type(e).__name__}: {_safe(e)} - attempt "
                  f"{attempt}/{max_attempts}, retrying in {wait:.1f}s...")
            _sleep(wait)
            continue
        if resp.status_code == 401 and isinstance(token, GraphAuth) and not refreshed:
            print(f"    [!] Graph {method} {_short(url)}: 401 - renewing the token and retrying...")
            token.invalidate()
            refreshed = True
            attempt -= 1          # a token renewal is not a failed attempt
            continue
        if resp.status_code in RETRYABLE_STATUS and attempt < max_attempts:
            wait = backoff_seconds(attempt, resp.headers.get("Retry-After"))
            print(f"    [!] Graph {method} {_short(url)}: HTTP {resp.status_code} - attempt "
                  f"{attempt}/{max_attempts}, retrying in {wait:.1f}s...")
            _sleep(wait)
            continue
        return resp


def _short(url):
    return url.replace(GRAPH_ROOT, "").split("?")[0][:120]


def _safe(e):
    """Error text without the upload-session URL's pre-authenticated query string."""
    return re.sub(r"\?[^\s'\"]*", "?***", str(e))[:300]


def _check(resp, what):
    if 200 <= resp.status_code < 300:
        return resp
    try:
        err = resp.json().get("error", {})
        detail = f"{err.get('code', '')}: {err.get('message', '')}"
    except Exception:
        detail = (resp.text or "")[:300]
    raise SharePointError(f"{what} failed: HTTP {resp.status_code} {detail}".rstrip(": "))


def _item_path(path):
    return quote(path.strip("/"), safe="/")


# ── site / drive / folders ───────────────────────────────────
def resolve_site_id(token, site_url):
    """Turn a SharePoint site URL into a Graph site id.
    e.g. https://contoso.sharepoint.com/sites/NasherMiles
    """
    parsed = urlparse(site_url)
    hostname = parsed.netloc                      # contoso.sharepoint.com
    site_path = parsed.path.rstrip("/")           # /sites/NasherMiles
    url = f"{GRAPH_ROOT}/sites/{hostname}:{site_path}"
    return _check(graph_request("GET", url, token), f"Resolving site {site_url}").json()["id"]


def resolve_drive_id(token, site_id):
    """Get the site's default document library (drive) id."""
    url = f"{GRAPH_ROOT}/sites/{site_id}/drive"
    return _check(graph_request("GET", url, token), "Resolving the document library").json()["id"]


def ensure_folder_path(token, drive_id, folder_path):
    """Ensure each segment of folder_path (e.g. 'Sales/Vinculum') exists, creating
    missing folders. A folder is created only when the lookup says 404, and with
    conflictBehavior 'fail' (409 = someone else created it = fine): an existing
    folder is never replaced."""
    segments = [s for s in folder_path.strip("/").split("/") if s]
    parent = ""
    for seg in segments:
        built = f"{parent}/{seg}" if parent else seg
        r = graph_request("GET", f"{GRAPH_ROOT}/drives/{drive_id}/root:/{_item_path(built)}", token)
        if r.status_code == 200:
            parent = built
            continue
        if r.status_code != 404:
            _check(r, f"Looking up folder {built}")
        create_url = (f"{GRAPH_ROOT}/drives/{drive_id}/root/children" if not parent else
                      f"{GRAPH_ROOT}/drives/{drive_id}/root:/{_item_path(parent)}:/children")
        body = {"name": seg, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
        cr = graph_request("POST", create_url, token, json=body)
        if cr.status_code != 409:
            _check(cr, f"Creating folder {built}")
        print(f"    [OK] Folder {built} {'already exists' if cr.status_code == 409 else 'created'}.")
        parent = built
    return folder_path.strip("/")


def list_folder_files(token, drive_id, folder_path):
    """{name: {"size": int, "lastModifiedDateTime": str}} of the files in folder_path
    ({} if the folder does not exist). Follows @odata.nextLink pages."""
    url = (f"{GRAPH_ROOT}/drives/{drive_id}/root:/{_item_path(folder_path)}:/children"
           "?$select=name,size,lastModifiedDateTime,file&$top=999")
    files = {}
    while url:
        r = graph_request("GET", url, token)
        if r.status_code == 404:
            return {}
        body = _check(r, f"Listing {folder_path}").json()
        for item in body.get("value", []):
            if "file" in item:
                files[item["name"]] = {"size": item.get("size"),
                                       "lastModifiedDateTime": item.get("lastModifiedDateTime")}
        url = body.get("@odata.nextLink")
    return files


# ── upload ───────────────────────────────────────────────────
def upload_file_to_sharepoint(token, drive_id, local_filepath, folder_path, filename=None):
    """Upload (or overwrite) a file into folder_path on the drive. Simple PUT up to
    4 MB, a resumable upload session (5 MB chunks) above. Each request is retried on
    transient errors. Raises SharePointError if SharePoint then reports an empty file.
    Returns the item's webUrl."""
    filename = filename or os.path.basename(local_filepath)
    dest = _item_path(f"{folder_path.strip('/')}/{filename}")
    size = os.path.getsize(local_filepath)

    if size <= SIMPLE_UPLOAD_LIMIT:
        with open(local_filepath, "rb") as f:
            data = f.read()
        r = graph_request("PUT", f"{GRAPH_ROOT}/drives/{drive_id}/root:/{dest}:/content", token,
                          headers={"Content-Type": "application/octet-stream"}, data=data,
                          timeout=120)
        item = _check(r, f"Uploading {filename}").json()
    else:
        s = graph_request("POST", f"{GRAPH_ROOT}/drives/{drive_id}/root:/{dest}:/createUploadSession",
                          token, json={"item": {"@microsoft.graph.conflictBehavior": "replace"}})
        upload_url = _check(s, f"Starting the upload session for {filename}").json()["uploadUrl"]
        item = None
        with open(local_filepath, "rb") as f:
            start = 0
            while start < size:
                chunk = f.read(UPLOAD_CHUNK_SIZE)
                end = start + len(chunk) - 1
                # uploadUrl is pre-authenticated: no Authorization header (token=None).
                r = graph_request("PUT", upload_url, None, data=chunk, timeout=300, headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                })
                _check(r, f"Uploading {filename} bytes {start}-{end}")
                if r.status_code in (200, 201):
                    item = r.json()
                start = end + 1
        if item is None:
            raise SharePointError(f"Upload of {filename} ended without the final item.")

    reported = item.get("size")
    if reported is not None and size and int(reported) == 0:
        raise SharePointError(f"{filename}: SharePoint reports 0 bytes, the local file has "
                              f"{size} - upload incomplete.")
    if reported is not None and int(reported) != size:
        # SharePoint may rewrite Office metadata (property demotion), so a small
        # difference is normal - logged, not an error.
        print(f"    [i] {filename}: SharePoint reports {reported} bytes (local {size}).")
    return item.get("webUrl")
