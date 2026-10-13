"""
Run one daily Vinculum report for every date that needs it (2026-10-13).

Pick Pack and the Sales Report used to export "Yesterday" only: a failed or skipped
run lost that day for good (Pick Pack 27.09.2026 is still missing) and nothing was
re-tried. They now follow the Order Enquiry rules (common/report_dates.py):
  REPORT_DATE blank -> catch-up: every day of the last DEFAULT_LOOKBACK_DAYS (ending
                       yesterday, IST) whose Drive file is missing
  REPORT_DATE=27/09/2026 | 11/10/2026,12/10/2026 | 11/10/2026-12/10/2026 -> those days
                       (re-exported unless SKIP_EXISTING=true)
Each date gets up to max_attempts tries (a fresh browser each); a Vinculum LOGIN
failure stops the whole run at once (no retry, no other dates - repeated failed
logins can lock the user). Exit code 1 if any date failed, so Actions shows it.

The browser part is the caller's export_fn(drive_service, day, attempt).
"""

import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

from common.report_dates import ReportDateError, dates_to_export, drive_filename, vinculum_date
from common.vinculum_ui import LoginError

IST = timezone(timedelta(hours=5, minutes=30))   # India: no DST


def env_true(name):
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "y")


def run_dates(days, export_fn, drive_service, name, max_attempts=2, pause_seconds=30,
              sleep=time.sleep):
    """Exports each day; returns {day: "OK (attempt n)" | "FAILED ..." | "NOT RUN ..."}."""
    results = {}
    for i, day in enumerate(days):
        print("\n" + "#" * 55)
        print(f"#  Report date {vinculum_date(day)} -> {drive_filename(day, *name)}")
        print("#" * 55)
        attempt = 1
        while True:
            try:
                export_fn(drive_service, day, attempt)
                results[day] = f"OK (attempt {attempt})"
                break
            except LoginError as e:
                print(f"\n[✗] {e}")
                results[day] = f"FAILED: {e}"
                for later in days[i + 1:]:
                    results[later] = "NOT RUN (Vinculum login failed)"
                return results
            except Exception as e:
                if not getattr(e, "quiet", False):
                    traceback.print_exc()
                print(f"\n[!] {vinculum_date(day)} attempt {attempt}/{max_attempts} failed: {e}")
                if attempt < max_attempts:
                    print(f"    Retrying with a fresh browser in {pause_seconds}s...")
                    sleep(pause_seconds)
                    attempt += 1
                    continue
                results[day] = f"FAILED after {attempt} attempt(s): {e}"
                break
    return results


def summarize(results, name):
    """Prints the summary; returns the exit code (1 if any date was not exported)."""
    print("\n" + "=" * 55)
    print("  SUMMARY")
    for day, res in results.items():
        print(f"  {drive_filename(day, *name)}: {res}")
    print("=" * 55)
    bad = [d for d, r in results.items() if not r.startswith("OK")]
    if bad:
        print(f"\n[✗] {len(bad)} date(s) not exported: "
              f"{', '.join(vinculum_date(d) for d in bad)} — the next scheduled run retries "
              f"the last week by itself; older days: run the workflow with report_date.")
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            try:
                with open(summary_path, "a", encoding="utf-8") as f:
                    f.write("### Not exported\n\n" + "\n".join(
                        f"- {drive_filename(d, *name)}: {results[d]}" for d in bad) + "\n")
            except OSError:
                pass
        return 1
    return 0


def main(title, name, folder_id, export_fn, get_drive_service, list_file_names,
         max_attempts=2):
    print("=" * 55)
    print(f"  Nasher Miles - {title} -> Google Drive")
    print("=" * 55)
    today = datetime.now(IST).date()
    spec = os.environ.get("REPORT_DATE", "")

    print("\n[0] Authenticating Google Drive API (service account)...")
    drive_service = get_drive_service()
    print("    [OK] Drive API ready!")

    existing = list_file_names(drive_service, folder_id, name[0] or None)
    try:
        days, mode = dates_to_export(spec, today, existing, name,
                                     skip_existing_requested=env_true("SKIP_EXISTING"))
    except ReportDateError as e:
        print(f"\n[✗] REPORT_DATE: {e}")
        sys.exit(2)
    print(f"\n    Mode: {mode}")
    if not days:
        print("\n[✓] Nothing to do — every date already has its file in Drive.")
        return
    print(f"    Exporting: {', '.join(vinculum_date(d) for d in days)} "
          f"(up to {max_attempts} attempts each)")
    results = run_dates(days, export_fn, drive_service, name, max_attempts=max_attempts)
    sys.exit(summarize(results, name))
