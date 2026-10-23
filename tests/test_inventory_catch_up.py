"""Inventory's evening catch-up run (2026-10-23): with INVENTORY_ONLY_IF_MISSING=true it
exports only when no file dated today (IST) is in Drive; otherwise it exits before
launching a browser (no Vinculum login)."""

import importlib.util
import os
import sys
from datetime import datetime

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
pytest.importorskip("DrissionPage")


@pytest.fixture
def inv(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "inventory_report_to_drive", os.path.join(ROOT, "scripts", "inventory_report_to_drive.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "LOGIN_ID", "u")
    monkeypatch.setattr(mod, "PASSWORD", "p")
    monkeypatch.setattr(mod, "LOCAL_DOWNLOAD_PATH", str(tmp_path))
    monkeypatch.setenv("GDRIVE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setattr(mod, "get_drive_service", lambda: object())

    class Launched(Exception):
        pass

    def no_browser(*a, **k):
        raise Launched()

    monkeypatch.setattr(mod, "ChromiumPage", no_browser)
    mod.Launched = Launched
    return mod


def _today_name(mod):
    return datetime.now(mod.IST).strftime("%d-%m-%Y_%H-%M")


def test_catch_up_skips_when_todays_file_exists(inv, monkeypatch):
    monkeypatch.setenv("INVENTORY_ONLY_IF_MISSING", "true")
    monkeypatch.setattr(inv, "list_file_names", lambda svc, folder, name: {_today_name(inv)})
    inv.main()                                 # returns without launching the browser


def test_catch_up_exports_when_todays_file_is_missing(inv, monkeypatch):
    monkeypatch.setenv("INVENTORY_ONLY_IF_MISSING", "true")
    monkeypatch.setattr(inv, "list_file_names", lambda svc, folder, name: {"01-01-2020_10-00"})
    with pytest.raises(inv.Launched):
        inv.main()


def test_catch_up_exports_when_drive_cannot_be_listed(inv, monkeypatch):
    monkeypatch.setenv("INVENTORY_ONLY_IF_MISSING", "true")
    monkeypatch.setattr(inv, "list_file_names", lambda svc, folder, name: None)
    with pytest.raises(inv.Launched):
        inv.main()


@pytest.mark.parametrize("flag", ["", "false"])
def test_morning_and_manual_runs_always_export(inv, monkeypatch, flag):
    monkeypatch.setenv("INVENTORY_ONLY_IF_MISSING", flag)
    monkeypatch.setattr(inv, "list_file_names",
                        lambda *a: pytest.fail("Drive must not be checked"))
    with pytest.raises(inv.Launched):
        inv.main()
