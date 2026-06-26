"""Phase 3 — the REAL config/strategy.yaml routes USDC to Bitget, USD1 to Bybit.

The unit-level resolver is covered in test_config_exchange_for.py (injected cfg); this
pins the SHIPPED config + the registry end-to-end: ``adapter_for("USDCUSDT")`` is a
BitgetAdapter (USDC@Bitget per the design) while ``adapter_for("USD1USDT")`` stays a
BybitAdapter (zero-change). This is what wires the running engine onto the right venue.

Run: PYTHONPATH=src python3 -m pytest tests/test_config_bitget_routing.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.config import exchange_for                         # noqa: E402
from sca.live.exchanges import adapter_for                  # noqa: E402
from sca.live.exchanges.bitget import BitgetAdapter         # noqa: E402
from sca.live.exchanges.bybit import BybitAdapter           # noqa: E402


def test_real_config_routes_usdc_to_bitget():
    # reads the loaded CFG (config/strategy.yaml) — no injected cfg.
    assert exchange_for("USDCUSDT") == "bitget"


def test_real_config_keeps_usd1_on_bybit():
    assert exchange_for("USD1USDT") == "bybit"


def test_adapter_for_usdc_is_bitget_end_to_end():
    assert isinstance(adapter_for("USDCUSDT"), BitgetAdapter)


def test_adapter_for_usd1_is_bybit_end_to_end():
    assert isinstance(adapter_for("USD1USDT"), BybitAdapter)


def test_production_creds_route_per_exchange_with_real_live_block():
    # The shipped live: block sets api_key_env=BYBIT_API_KEY. That Bybit-named flat
    # override must NOT leak into the Bitget route — the USDC@Bitget engine must read
    # BITGET_* (key/secret/passphrase). This pins the real config + resolver together so
    # a live Bitget deploy can never silently auth with Bybit env names.
    from sca.config import CFG
    from sca.live import creds
    live = CFG.get("live", {})

    bg = exchange_for("USDCUSDT")
    assert creds.credential_env_names(live, exchange=bg) == (
        "BITGET_API_KEY", "BITGET_API_SECRET")
    assert creds.passphrase_env_name(live, exchange=bg) == "BITGET_API_PASSPHRASE"

    by = exchange_for("USD1USDT")
    assert creds.credential_env_names(live, exchange=by) == (
        "BYBIT_API_KEY", "BYBIT_API_SECRET")
    assert creds.passphrase_env_name(live, exchange=by) is None   # Bybit has no passphrase
