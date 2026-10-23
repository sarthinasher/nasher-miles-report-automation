"""Pending Report request times are IST: parseTs must return the real epoch whatever
the browser's time zone (2026-10-23, see "Errors and their fixes/
2026-10-23-pending-report-time-filter-ist.md"). Runs the JS with node (on the
ubuntu-latest runners); skipped where node is not installed."""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from common.vinculum_ui import PARSE_IST_TIMESTAMP_JS  # noqa: E402

NODE = shutil.which("node")


def _parse_ts_in_node(texts, tz):
    script = PARSE_IST_TIMESTAMP_JS + f"\nconsole.log(JSON.stringify({json.dumps(texts)}.map(parseTs)));"
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True,
                         env={**os.environ, "TZ": tz}, timeout=30)
    return json.loads(out.stdout)


def _ms(*args):
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize("tz", ["UTC", "Asia/Kolkata", "America/New_York"])
def test_ist_request_time_is_read_as_ist_in_any_browser_time_zone(tz):
    got = _parse_ts_in_node([
        "23/10/2026 02:03 PM",      # Pick Pack run 39925372015 logged this at 08:33:31 UTC
        "01/11/2026 12:10 AM",      # just after midnight IST = previous day in UTC
        "31/10/2026 12:00 PM",      # noon, month end
        "no time here",
    ], tz)
    assert got == [_ms(2026, 10, 23, 8, 33), _ms(2026, 10, 31, 18, 40),
                   _ms(2026, 10, 31, 6, 30), 0]


def test_report_scripts_use_the_shared_ist_parser():
    for name in ("order_enquiry_report_to_drive.py", "sku_master_report_to_drive.py"):
        with open(os.path.join(ROOT, "scripts", name), encoding="utf-8") as f:
            src = f.read()
        assert "{PARSE_IST_TIMESTAMP_JS}" in src, name
        assert "new Date(y, mo-1, d, hh, mm" not in src, name
