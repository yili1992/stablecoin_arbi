"""Tests for the zero-dependency dashboard HTTP surface."""
import gzip
import json
import os
import shutil
import subprocess
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
    assert "数据加载失败" in dashboard.DASHBOARD_JS
    assert "每 60 秒自动刷新" in dashboard.PAGE
    assert "setInterval(function(){if(!document.hidden)tick();},60000)" in dashboard.DASHBOARD_JS


def test_dashboard_js_can_be_gzipped_below_shell_truncation_size():
    body, ctype, headers = dashboard._asset_response("/dashboard.js", {"Accept-Encoding": "gzip"})
    assert ctype == "application/javascript; charset=utf-8"
    assert headers["Content-Encoding"] == "gzip"
    assert len(body) < 10_000
    assert b"/api/status" in gzip.decompress(body)


def test_large_status_response_can_be_gzipped_below_truncation_size(tmp_path):
    doc = {
        "symbol": "USD1USDT",
        "mode": "live",
        "events": [{"ts": i, "side": "buy", "price": 1.0001, "qty": 1.0} for i in range(160)],
        "klines": [{"t": i, "o": 1.0, "h": 1.0002, "l": 0.9999, "c": 1.0001} for i in range(120)],
        "history": [{"t": i, "equity": 999.6784, "rt30": 0.9998} for i in range(600)],
    }
    status_path = tmp_path / "status_USD1USDT_live.json"
    status_path.write_text(json.dumps(doc))

    raw = json.dumps(dashboard._read_status(str(tmp_path), mode="live")).encode("utf-8")
    body, ctype, headers = dashboard._status_response(str(tmp_path), {"Accept-Encoding": "gzip"}, mode="live")

    assert len(raw) > 20_000
    assert ctype == "application/json; charset=utf-8"
    assert headers["Content-Encoding"] == "gzip"
    assert len(body) < 10_000
    assert json.loads(gzip.decompress(body))["USD1USDT_live"]["mode"] == "live"


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


def test_dashboard_js_does_not_hardcode_usd1_holdings_label():
    """Regression: the base-coin holdings label must be derived per-symbol, never
    the literal "USD1 持有" (which leaked onto the USDC card as "USD1 持有 0.0%").
    The quote-leg label likewise derives from the per-symbol quote coin."""
    js = dashboard.DASHBOARD_JS
    assert "USD1 持有" not in js          # no hard-coded base-coin label
    assert "USDT 空闲" not in js          # no hard-coded quote-coin label
    assert "s.base" in js                 # reads the per-symbol base coin
    assert "s.quote" in js                # reads the per-symbol quote coin


# ---- node+vm render harness: load the REAL DASHBOARD_JS and call card() ----
# card() is a pure string builder (drawChart/miniChart touch the DOM but run only
# inside render(), not card()), so a minimal global shim is enough to evaluate it.
_NODE_HARNESS = r'''
const fs=require('fs'), vm=require('vm');
const js=fs.readFileSync(process.argv[2],'utf8');
const doc=JSON.parse(process.argv[3]);
const sandbox={console:console, document:{getElementById:function(){return null;}},
  fetch:function(){return Promise.reject(new Error('no-net-in-test'));},
  setInterval:function(){return 0;}, setTimeout:function(){return 0;}};
sandbox.window=sandbox;
sandbox.window.addEventListener=function(){};
sandbox.window.devicePixelRatio=1;
vm.createContext(sandbox);
vm.runInContext(js+'\n;globalThis.__card=card;', sandbox);
process.stdout.write(String(sandbox.__card(doc)));
'''


def _render_card(tmp_path, doc):
    node = shutil.which("node")
    (tmp_path / "dash.js").write_text(dashboard.DASHBOARD_JS, encoding="utf-8")
    (tmp_path / "harness.js").write_text(_NODE_HARNESS, encoding="utf-8")
    res = subprocess.run(
        [node, str(tmp_path / "harness.js"), str(tmp_path / "dash.js"), json.dumps(doc)],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, f"node render failed:\n{res.stderr}"
    return res.stdout


def test_card_renders_per_symbol_coin_labels_via_node(tmp_path):
    """Run the REAL card() in node+vm and assert holdings labels reflect the
    per-symbol base coin: the USDC card must NOT show "USD1 持有" (the reported
    bug); the USD1 card must still show "USD1 持有"; and a legacy status doc that
    predates the base/quote fields must still derive the coin from the symbol."""
    if not shutil.which("node"):
        import pytest
        pytest.skip("node not available")

    usdc = _render_card(tmp_path, {
        "symbol": "USDCUSDT", "base": "USDC", "quote": "USDT",
        "position": {"usd1_pct": 0.0, "n_in_usd1": 0, "n_in_usdt": 5, "total_value": 100.0,
                     "slices": [{"i": 0, "state": "usdt", "frac": 0.2, "qty": 0, "cash": 20.0}]},
    })
    assert "USDC 持有" in usdc          # deployment panel uses the real base coin
    assert "USD1 持有" not in usdc      # the bug: no hard-coded USD1 on the USDC card
    assert "USDT 空闲" in usdc          # idle slice + quote leg still labelled USDT

    usd1 = _render_card(tmp_path, {
        "symbol": "USD1USDT", "base": "USD1", "quote": "USDT",
        "position": {"usd1_pct": 60.0, "n_in_usd1": 1, "n_in_usdt": 0, "total_value": 100.0,
                     "slices": [{"i": 0, "state": "usd1", "frac": 0.2, "qty": 20.0,
                                 "cash": 0.0, "entry": 1.0, "sell_target": 1.0001,
                                 "value_usd": 20.0}]},
    })
    assert "USD1 持有" in usd1

    # legacy/stale status doc (written before this fix) lacks base/quote -> the
    # front-end must still derive the coin from the symbol (zero-surprise).
    legacy = _render_card(tmp_path, {
        "symbol": "USDCUSDT",
        "position": {"usd1_pct": 0.0,
                     "slices": [{"i": 0, "state": "usdt", "frac": 1.0, "qty": 0, "cash": 10.0}]},
    })
    assert "USDC 持有" in legacy
    assert "USD1 持有" not in legacy
