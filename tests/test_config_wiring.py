"""Tests that engine/tools read launch params from the consolidated runtime: block
(config-consolidation C3). Behavior nuances locked here:
  - engine launch defaults come from runtime: (NOT the dryrun: measurement block)
  - out_dir precedence: csv dirname > SCA_OUT_DIR > runtime.out_dir > caller fallback
  - no default shift: bare paper engine with no csv/env keeps out_dir "." (no orphaned state)

ISOLATION: offline — PaperEngine __init__ does no network (bootstrap is in run()).

Run: PYTHONPATH=src python3 -m pytest tests/test_config_wiring.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import sca.live.engine as engine  # noqa: E402
from sca.live.engine import PaperEngine  # noqa: E402


def test_engine_launch_defaults_from_runtime_not_dryrun():
    # runtime.seconds is 604800; the dryrun: measurement block's 86400 must NOT leak in
    assert engine.DEFAULT_SECONDS == 604800
    assert engine.DEFAULT_SYMBOL == "USD1USDT"


def test_engine_out_dir_no_csv_keeps_dot_fallback(monkeypatch):
    monkeypatch.delenv("SCA_OUT_DIR", raising=False)
    eng = PaperEngine(symbol="USD1USDT", mode="paper", seconds=1, csv_path=None)
    assert eng.out_dir == "."          # preserved — no shift to ./out (Codex P1)


def test_engine_out_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SCA_OUT_DIR", str(tmp_path))
    eng = PaperEngine(symbol="USD1USDT", mode="paper", seconds=1, csv_path=None)
    assert eng.out_dir == str(tmp_path)


def test_engine_out_dir_csv_dirname_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("SCA_OUT_DIR", "/should/be/ignored")
    csv = tmp_path / "sub" / "x.csv"
    (tmp_path / "sub").mkdir()
    eng = PaperEngine(symbol="USD1USDT", mode="paper", seconds=1, csv_path=str(csv))
    assert eng.out_dir == str(tmp_path / "sub")   # csv dirname has top precedence
