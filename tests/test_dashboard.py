"""Tests for the zero-dependency dashboard HTTP surface."""
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.tools import dashboard  # noqa: E402


def test_page_is_small_shell_and_loads_dashboard_script():
    # Regression: serving one large inline HTML+JS response was observed truncating mid
    # script on the deployed server, leaving only the dark background. Keep the document
    # shell small and load the heavier renderer as a separate script resource.
    body = dashboard.PAGE.encode("utf-8")
    assert len(body) < 10_000
    assert 'src="/dashboard.js"' in dashboard.PAGE
    assert "/api/status" not in dashboard.PAGE
    assert "/api/status" in dashboard.DASHBOARD_JS
    assert "每 60 秒自动刷新" in dashboard.PAGE
    assert "setInterval(function(){if(!document.hidden)tick();},60000)" in dashboard.DASHBOARD_JS


def test_dashboard_js_can_be_gzipped_below_shell_truncation_size():
    body, ctype, headers = dashboard._asset_response("/dashboard.js", {"Accept-Encoding": "gzip"})
    assert ctype == "application/javascript; charset=utf-8"
    assert headers["Content-Encoding"] == "gzip"
    assert len(body) < 10_000
    assert b"/api/status" in gzip.decompress(body)


def _write_status(out_dir, stem, mode):
    with open(os.path.join(out_dir, f"status_{stem}.json"), "w") as fh:
        json.dump({"symbol": "USD1USDT", "mode": mode}, fh)


def test_read_status_live_mode_filters_out_stale_dryrun_and_legacy_files(tmp_path):
    _write_status(tmp_path, "USD1USDT", "paper")
    _write_status(tmp_path, "USD1USDT_dryrun", "dryrun")
    _write_status(tmp_path, "USD1USDT_live", "live")

    out = dashboard._read_status(str(tmp_path), mode="live")

    assert set(out) == {"USD1USDT_live"}
    assert out["USD1USDT_live"]["mode"] == "live"


def test_read_status_dryrun_mode_filters_out_stale_live_and_legacy_files(tmp_path):
    _write_status(tmp_path, "USD1USDT", "paper")
    _write_status(tmp_path, "USD1USDT_dryrun", "dryrun")
    _write_status(tmp_path, "USD1USDT_live", "live")

    out = dashboard._read_status(str(tmp_path), mode="dryrun")

    assert set(out) == {"USD1USDT_dryrun"}
    assert out["USD1USDT_dryrun"]["mode"] == "dryrun"
