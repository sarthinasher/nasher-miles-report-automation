"""2026-11-23: tries a switch to EVERY location the login is offered (no export) and prints
RESULT A02=BROKEN A03=OK ...; logs in again after a failed switch (it ends the session).
Run: probe workflow, mode=dry_run, report=switchall. Use the BROKEN list for VIN_UNSWITCHABLE."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.vinculum_ui import launch_browser, vinculum_login, read_locations, switch_location, LocationError

BASE = "https://nashermiles.vineretail.com/eRetailWeb"
L, P, O = os.environ["VIN_LOGIN_ID"], os.environ["VIN_PASSWORD"], os.environ.get("VIN_ORG_ID", "")
page = launch_browser("/tmp/vinculum_probe/dl")
vinculum_login(page, BASE, L, P, O)
codes = [c.strip().upper() for c, _ in read_locations(page, BASE)["options"]]
res = {}
for c in codes:
    try:
        switch_location(page, BASE, c, tries=1)
        res[c] = "OK"
    except LocationError as e:
        res[c] = "BROKEN"
        print(f"    -> {c} broken ({e}); logging in again")
        try:
            page.quit()
        except Exception:
            pass
        page = launch_browser("/tmp/vinculum_probe/dl")
        vinculum_login(page, BASE, L, P, O)
print("RESULT " + " ".join(f"{k}={v}" for k, v in res.items()))
