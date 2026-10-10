"""
Pure date logic for the daily Vinculum report scripts (no browser, no Google).

- Which report date(s) a run should export: a manual `report_date` input
  (one date, a comma list, or an inclusive range), or — when no input is given —
  a catch-up window of the last few days ending yesterday (IST), so a failed or
  delayed run is picked up by the next one instead of losing that day.
- The Drive file name for a report date (always the REPORT date, never the run date).
"""

from datetime import datetime, timedelta

DATE_FMT = "%d/%m/%Y"          # what the workflow input and Vinculum's date picker use
MAX_RANGE_DAYS = 31            # refuse accidental huge back-fills (one export per day)
DEFAULT_LOOKBACK_DAYS = 3      # scheduled runs: yesterday + the 2 days before it


class ReportDateError(ValueError):
    """Bad report_date input. The message is shown in the Actions log."""


def _parse_one(text):
    text = text.strip()
    try:
        return datetime.strptime(text, DATE_FMT).date()
    except ValueError:
        raise ReportDateError(
            f"Invalid date {text!r} — use DD/MM/YYYY, e.g. 08/10/2026") from None


def parse_report_dates(spec, today):
    """Turns the `report_date` input into a sorted list of distinct dates.

    spec: "" / None -> []  (caller then uses default_window)
          "08/10/2026"                     -> [8 Oct]
          "06/10/2026,08/10/2026"          -> [6 Oct, 8 Oct]
          "06/10/2026-09/10/2026"          -> [6, 7, 8, 9 Oct]  (inclusive range)
    today: the current IST date. Today and future dates are rejected (the day is
    not complete yet, so its export would be partial).
    """
    spec = (spec or "").strip()
    if not spec:
        return []

    days = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bits = part.split("-")
            if len(bits) != 2:
                raise ReportDateError(f"Invalid range {part!r} — use DD/MM/YYYY-DD/MM/YYYY")
            start, end = _parse_one(bits[0]), _parse_one(bits[1])
            if start > end:
                raise ReportDateError(f"Range {part!r} starts after it ends")
            n = (end - start).days + 1
            if n > MAX_RANGE_DAYS:
                raise ReportDateError(
                    f"Range {part!r} is {n} days — max {MAX_RANGE_DAYS} per run")
            days.update(start + timedelta(days=i) for i in range(n))
        else:
            days.add(_parse_one(part))

    if not days:
        raise ReportDateError(f"No dates in {spec!r}")
    if len(days) > MAX_RANGE_DAYS:
        raise ReportDateError(f"{len(days)} dates requested — max {MAX_RANGE_DAYS} per run")
    future = sorted(d for d in days if d >= today)
    if future:
        raise ReportDateError(
            "Report date must be before today (the day must be complete): "
            + ", ".join(d.strftime(DATE_FMT) for d in future))
    return sorted(days)


def default_window(today, lookback_days=DEFAULT_LOOKBACK_DAYS):
    """Dates a scheduled run considers: the `lookback_days` days ending yesterday,
    oldest first. Days whose file already exists in Drive are skipped by the
    caller (see pending_dates), so normally only yesterday is exported."""
    if lookback_days < 1:
        raise ValueError("lookback_days must be >= 1")
    return [today - timedelta(days=i) for i in range(lookback_days, 0, -1)]


def drive_filename(report_day, prefix="OrderEnquiry_"):
    """OrderEnquiry_DD-MM-YYYY for the REPORT date (not the run date)."""
    return prefix + report_day.strftime("%d-%m-%Y")


def vinculum_date(report_day):
    return report_day.strftime(DATE_FMT)


def pending_dates(days, existing_names, prefix="OrderEnquiry_"):
    """Drops the days whose Drive file already exists (existing_names: set of names)."""
    return [d for d in days if drive_filename(d, prefix) not in existing_names]


def should_retry(attempt, max_attempts, error_retryable=True):
    """True if a failed attempt (1-based) of one report date should be retried
    with a fresh browser session. Config errors (bad credentials, missing
    secrets) are not retryable — retrying cannot fix them."""
    return error_retryable and attempt < max_attempts
