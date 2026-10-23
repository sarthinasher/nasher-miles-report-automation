"""Drive upload retries (2026-10-23): "EOF occurred in violation of protocol" on the
first Drive call after a long browser export failed the date at once."""
import os
import ssl
import sys

import httplib2
import pytest
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import common.drive_utils as du  # noqa: E402
from common.report_merge import remove_attempt_files  # noqa: E402


def http_error(status, content=b"{}"):
    return HttpError(httplib2.Response({"status": status}), content)


class FakeRequest:
    def __init__(self, outcomes):
        self.outcomes = outcomes            # shared list: exception or result per next_chunk

    def next_chunk(self):
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


class FakeCall:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeFiles:
    def __init__(self, svc):
        self.svc = svc

    def list(self, **kw):
        self.svc.calls.append(("list", kw["q"]))

        def run():
            if self.svc.list_errors:
                raise self.svc.list_errors.pop(0)
            return {"files": list(self.svc.existing)}
        return FakeCall(run)

    def create(self, **kw):
        self.svc.calls.append(("create", kw["body"]["name"]))
        assert kw["media_body"].resumable()
        assert kw["media_body"].chunksize() % (256 * 1024) == 0
        return FakeRequest(self.svc.chunks)

    def update(self, **kw):
        self.svc.calls.append(("update", kw["fileId"]))
        return FakeRequest(self.svc.chunks)


class FakeService:
    def __init__(self, chunks, existing=(), list_errors=()):
        self.chunks = list(chunks)
        self.existing = list(existing)
        self.list_errors = list(list_errors)
        self.calls = []

    def files(self):
        return FakeFiles(self)


@pytest.fixture
def report(tmp_path):
    p = tmp_path / "20.10.2026.xlsx"
    p.write_bytes(b"PK fake xlsx")
    return str(p)


def no_sleep(log):
    return lambda s: log.append(s)


def test_ssl_eof_on_a_chunk_is_retried_on_a_rebuilt_client(report):
    old = FakeService([ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:2437)")])
    new = FakeService([(None, None), (None, {"id": "NEW_ID"})])     # two chunks
    waits = []
    file_id = du.upload_to_drive(old, report, "20.10.2026", "FOLDER", sleep=no_sleep(waits),
                                 rebuild=lambda: new)
    assert file_id == "NEW_ID"
    assert len(waits) == 1 and 2 <= waits[0] < 3                     # first backoff ~2 s
    assert [c[0] for c in old.calls] == ["list", "create"]
    assert [c[0] for c in new.calls] == ["list", "create"]


def test_ssl_eof_on_the_first_call_then_success_updates_existing(report):
    svc = FakeService([(None, {"id": "FID"})], existing=[{"id": "FID", "name": "20.10.2026"}],
                      list_errors=[ssl.SSLEOFError(8, "EOF occurred in violation of protocol")])
    waits = []
    assert du.upload_to_drive(svc, report, "20.10.2026", "F", sleep=no_sleep(waits),
                              rebuild=lambda: None) == "FID"
    assert ("update", "FID") in svc.calls and len(waits) == 1


def test_gives_up_after_max_attempts(report):
    errors = [ConnectionResetError(104, "Connection reset by peer") for _ in range(10)]
    svc = FakeService([], list_errors=errors)
    waits = []
    assert du.upload_to_drive(svc, report, "x", "F", sleep=no_sleep(waits), rebuild=None,
                              max_attempts=4) is None
    assert len([c for c in svc.calls if c[0] == "list"]) == 4
    assert len(waits) == 3
    assert waits == sorted(waits)                                    # growing backoff


def test_default_attempts_wait_well_under_three_minutes():
    total = sum(du.backoff_seconds(n, rand=lambda: 1.0) for n in range(1, du.DRIVE_MAX_ATTEMPTS))
    assert du.DRIVE_MAX_ATTEMPTS >= 5 and total < 120


@pytest.mark.parametrize("status", [400, 401, 404])
def test_permanent_http_errors_are_not_retried(report, status):
    svc = FakeService([], list_errors=[http_error(status)])
    waits = []
    assert du.upload_to_drive(svc, report, "x", "F", sleep=no_sleep(waits), rebuild=None) is None
    assert len(svc.calls) == 1 and waits == []


def test_permission_403_is_not_retried_but_rate_limit_403_is():
    assert not du.is_transient_drive_error(
        http_error(403, b'{"error": {"errors": [{"reason": "insufficientFilePermissions"}]}}'))
    assert du.is_transient_drive_error(
        http_error(403, b'{"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}'))
    assert du.is_transient_drive_error(
        http_error(403, b'{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}'))


@pytest.mark.parametrize("err", [
    ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:2437)"),
    ssl.SSLError("bad record mac"),
    ConnectionResetError(104, "reset"), BrokenPipeError(32, "pipe"), TimeoutError("timed out"),
    __import__("http.client").client.RemoteDisconnected("Remote end closed connection"),
    __import__("http.client").client.IncompleteRead(b"x"),
    httplib2.ServerNotFoundError("Unable to find the server"),
    OSError("EOF occurred in violation of protocol"),
    http_error(429), http_error(500), http_error(502), http_error(503), http_error(504),
])
def test_transient_errors(err):
    assert du.is_transient_drive_error(err)


@pytest.mark.parametrize("err", [ValueError("bad"), FileNotFoundError("no file"), http_error(400),
                                 http_error(404)])
def test_not_transient(err):
    assert not du.is_transient_drive_error(err)


def test_list_file_names_retries_then_returns_names():
    svc = FakeService([], existing=[{"name": "A"}, {"name": "B"}],
                      list_errors=[ssl.SSLEOFError(8, "EOF occurred in violation of protocol")])
    waits = []
    assert du.list_file_names(svc, "F", "", sleep=no_sleep(waits)) == {"A", "B"}
    assert len(waits) == 1


# ── after a failed upload the retry reuses the per-location files ──
def test_upload_failure_keeps_the_location_files_for_the_retry(tmp_path):
    a, b, merged = tmp_path / "a.csv", tmp_path / "b.csv", tmp_path / "merged.csv"
    for p in (a, b, merged):
        p.write_text("x")
    remove_attempt_files([("A03", str(a)), ("NM4", str(b)), ("S01", None)], str(merged),
                         keep_parts=True)
    assert a.exists() and b.exists() and not merged.exists()
    remove_attempt_files([("A03", str(a)), ("NM4", str(b))], None, keep_parts=False)
    assert not a.exists() and not b.exists()


def test_merged_file_that_is_a_location_file_is_kept_after_an_upload_failure(tmp_path):
    a = tmp_path / "a.xlsx"
    a.write_text("x")
    remove_attempt_files([("A02", str(a))], str(a), keep_parts=True)
    assert a.exists()
