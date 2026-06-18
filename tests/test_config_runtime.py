"""Tests for the consolidated config resolvers (sca.config) — config-consolidation C1.

Single source: config/strategy.yaml. These resolvers give the precedence
env > runtime: block > caller fallback, so launch params live in ONE place while
docker/env can still override. Secrets are NOT handled here (they stay env-only).

ISOLATION: pure — cfg + env injected; no real file/env dependence.

Run: PYTHONPATH=src python3 -m pytest tests/test_config_runtime.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca import config  # noqa: E402


# --- runtime() ---------------------------------------------------------------

def test_runtime_defaults_when_block_absent():
    assert config.runtime({}) == {
        "symbol": "USD1USDT", "seconds": 604800, "mode": "paper", "dashboard_port": 3015}


def test_runtime_reads_yaml_values():
    cfg = {"runtime": {"symbol": "USDEUSDT", "seconds": 100, "mode": "live", "dashboard_port": 3016}}
    assert config.runtime(cfg) == {
        "symbol": "USDEUSDT", "seconds": 100, "mode": "live", "dashboard_port": 3016}


# --- out_dir(fallback, cfg): env > runtime.out_dir > fallback -----------------

def test_out_dir_env_wins(monkeypatch):
    monkeypatch.setenv("SCA_OUT_DIR", "/x/out")
    assert config.out_dir(".", cfg={"runtime": {"out_dir": "./y"}}) == "/x/out"


def test_out_dir_runtime_when_no_env(monkeypatch):
    monkeypatch.delenv("SCA_OUT_DIR", raising=False)
    assert config.out_dir(".", cfg={"runtime": {"out_dir": "./y"}}) == "./y"


def test_out_dir_fallback_preserved_per_caller(monkeypatch):
    monkeypatch.delenv("SCA_OUT_DIR", raising=False)
    # runtime.out_dir UNSET -> caller's own fallback (no default shift, Codex P1)
    assert config.out_dir("./out", cfg={"runtime": {}}) == "./out"
    assert config.out_dir(".", cfg={}) == "."


# --- resolve_mode(): env MODE > runtime.mode > paper; bad -> paper (safe) -----

def test_resolve_mode_env_wins(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    assert config.resolve_mode(cfg={"runtime": {"mode": "paper"}}) == "live"


def test_resolve_mode_runtime_when_no_env(monkeypatch):
    monkeypatch.delenv("MODE", raising=False)
    assert config.resolve_mode(cfg={"runtime": {"mode": "live"}}) == "live"


def test_resolve_mode_default_paper(monkeypatch):
    monkeypatch.delenv("MODE", raising=False)
    assert config.resolve_mode(cfg={}) == "paper"


def test_resolve_mode_bad_value_coerces_paper(monkeypatch):
    # never let a typo'd MODE accidentally arm live — coerce unknown to paper
    monkeypatch.setenv("MODE", "garbage")
    assert config.resolve_mode(cfg={}) == "paper"


# --- resolve_testnet(): SINGLE source — env > runtime.testnet > live.testnet > False (F13) ---

def test_resolve_testnet_env_over_runtime_over_default():
    # env override beats the yaml runtime block
    assert config.resolve_testnet(cfg={"runtime": {"testnet": False}},
                                  env={"BYBIT_TESTNET": "true"}) is True
    # runtime.testnet when no env override
    assert config.resolve_testnet(cfg={"runtime": {"testnet": True}}, env={}) is True
    assert config.resolve_testnet(cfg={"runtime": {"testnet": False}}, env={}) is False
    # default False when neither env nor any config key is set
    assert config.resolve_testnet(cfg={}, env={}) is False


def test_live_testnet_redirects_to_runtime_testnet():
    # runtime.testnet is the SINGLE source; the deprecated live.testnet is redirected
    # (read) so old configs still resolve — but it must never create a split-brain knob.
    # Only live.testnet set -> redirected through the one resolver.
    assert config.resolve_testnet(cfg={"live": {"testnet": True}}, env={}) is True
    # runtime.testnet takes precedence over the deprecated live.testnet (no split-brain)
    assert config.resolve_testnet(
        cfg={"runtime": {"testnet": False}, "live": {"testnet": True}}, env={}) is False
    assert config.resolve_testnet(
        cfg={"runtime": {"testnet": True}, "live": {"testnet": False}}, env={}) is True


def test_resolve_testnet_env_false_overrides_runtime_true():
    # an explicit falsey env value still wins over a True runtime block
    assert config.resolve_testnet(cfg={"runtime": {"testnet": True}},
                                  env={"BYBIT_TESTNET": "0"}) is False


def test_resolve_testnet_unknown_env_falls_through_to_config():
    # a garbage/empty env value counts as UNSET (never an accidental True) and falls
    # through to the next precedence tier rather than coercing.
    assert config.resolve_testnet(cfg={"runtime": {"testnet": True}},
                                  env={"BYBIT_TESTNET": "garbage"}) is True
    assert config.resolve_testnet(cfg={"runtime": {"testnet": False}},
                                  env={"BYBIT_TESTNET": ""}) is False


def test_resolve_testnet_accepts_bool_env_value():
    # a non-string truthiness (e.g. an already-parsed bool) is honored directly
    assert config.resolve_testnet(cfg={"runtime": {"testnet": False}},
                                  env={"BYBIT_TESTNET": True}) is True


# --- resolve_maker_enabled(): env > runtime.maker_enabled > default false (C-P1#14) ----------

def test_resolve_maker_enabled_precedence():
    # env override beats runtime block
    assert config.resolve_maker_enabled(cfg={"runtime": {"maker_enabled": False}},
                                        env={"MAKER_ENABLED": "true"}) is True
    # runtime.maker_enabled when no env
    assert config.resolve_maker_enabled(cfg={"runtime": {"maker_enabled": True}}, env={}) is True
    assert config.resolve_maker_enabled(cfg={"runtime": {"maker_enabled": False}}, env={}) is False
    # default FALSE when neither set (rollback knob defaults OFF -> paper path)
    assert config.resolve_maker_enabled(cfg={}, env={}) is False
    # env false overrides runtime true
    assert config.resolve_maker_enabled(cfg={"runtime": {"maker_enabled": True}},
                                        env={"MAKER_ENABLED": "false"}) is False
