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
        "symbol": "USD1USDT", "seconds": 604800, "mode": "dryrun", "dashboard_port": 3015}


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


# --- resolve_mode(): env MODE > runtime.mode > dryrun; bad -> dryrun (safe, D14) -----

def test_resolve_mode_env_wins(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    assert config.resolve_mode(cfg={"runtime": {"mode": "dryrun"}}) == "live"


def test_resolve_mode_runtime_when_no_env(monkeypatch):
    monkeypatch.delenv("MODE", raising=False)
    assert config.resolve_mode(cfg={"runtime": {"mode": "live"}}) == "live"


def test_resolve_mode_default_dryrun(monkeypatch):
    monkeypatch.delenv("MODE", raising=False)
    assert config.resolve_mode(cfg={}) == "dryrun"


def test_resolve_mode_bad_value_coerces_dryrun(monkeypatch):
    # never let a typo'd MODE accidentally select live — coerce unknown to dryrun (D14)
    monkeypatch.setenv("MODE", "garbage")
    assert config.resolve_mode(cfg={}) == "dryrun"
    # the legacy "paper" value is no longer valid -> coerces to the safe default too
    monkeypatch.setenv("MODE", "paper")
    assert config.resolve_mode(cfg={}) == "dryrun"
