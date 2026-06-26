"""per-symbol deployment cap resolver — config.max_alloc_for(symbol).

Reads ``universe[symbol].max_total_alloc_usd`` if set, else the global
``live.max_total_alloc_usd`` fallback, else -1 (no cap = full wallet). The ONLY
real-money fund limit on a spot account (capital deployed = loss ceiling).
``cfg`` injectable for tests. Mirrors config.exchange_for.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from sca.config import max_alloc_for

CFG = {
    "universe": [
        {"symbol": "USD1USDT", "apr": 0.08, "kind": "reserve",
         "max_total_alloc_usd": 1000},
        {"symbol": "USDCUSDT", "apr": 0.0, "kind": "reserve",
         "exchange": "bitget", "max_total_alloc_usd": 400},
        {"symbol": "USDEUSDT", "apr": 0.035, "kind": "synthetic"},  # no per-symbol cap
    ],
    "live": {"max_total_alloc_usd": 777},
}


def test_reads_per_symbol_cap_usd1():
    assert max_alloc_for("USD1USDT", cfg=CFG) == 1000.0


def test_reads_per_symbol_cap_usdc():
    assert max_alloc_for("USDCUSDT", cfg=CFG) == 400.0


def test_falls_back_to_global_live_when_symbol_has_no_cap():
    # USDEUSDT has no per-symbol max_total_alloc_usd -> global live fallback.
    assert max_alloc_for("USDEUSDT", cfg=CFG) == 777.0


def test_unknown_symbol_falls_back_to_global_live():
    assert max_alloc_for("NOSUCHUSDT", cfg=CFG) == 777.0


def test_minus1_when_no_per_symbol_and_no_global():
    cfg = {"universe": [{"symbol": "USD1USDT", "apr": 0.08}]}
    assert max_alloc_for("USD1USDT", cfg=cfg) == -1.0


def test_empty_cfg_defaults_minus1():
    assert max_alloc_for("USD1USDT", cfg={}) == -1.0


def test_returns_float_even_when_yaml_gives_int():
    # YAML ints must come back as float (engine does float cap math).
    assert isinstance(max_alloc_for("USD1USDT", cfg=CFG), float)
    assert isinstance(max_alloc_for("USDEUSDT", cfg=CFG), float)


def test_per_symbol_zero_is_honored_not_treated_as_unset():
    # An explicit 0 cap (deploy nothing) must NOT fall through to the global.
    cfg = {"universe": [{"symbol": "USD1USDT", "max_total_alloc_usd": 0}],
           "live": {"max_total_alloc_usd": 777}}
    assert max_alloc_for("USD1USDT", cfg=cfg) == 0.0
