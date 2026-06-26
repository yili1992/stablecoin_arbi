"""Phase 3 — docker-compose bot-usdc runs the USDC engine against Bitget.

The USDC engine instance (``bot-usdc``) trades on Bitget (per the design), so its LIVE
credentials MUST be the BITGET_* env (key/secret/passphrase), not the Bybit ones the
USD1 ``bot`` uses. This pins the shipped compose file so a deploy can't accidentally
feed Bybit keys to the Bitget engine. The compose YAML must also stay valid.

Run: PYTHONPATH=src python3 -m pytest tests/test_docker_compose_bitget.py -q
"""
import os

import pytest

yaml = pytest.importorskip("yaml")

_COMPOSE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docker-compose.yml")


def _load():
    with open(_COMPOSE) as f:
        return yaml.safe_load(f)


def test_compose_is_valid_yaml():
    doc = _load()
    assert "services" in doc and "bot-usdc" in doc["services"]


def test_bot_usdc_runs_usdcusdt_symbol():
    svc = _load()["services"]["bot-usdc"]
    assert svc["command"] == ["paper", "--symbol", "USDCUSDT"]


def test_bot_usdc_uses_bitget_credentials():
    # the environment block must inject BITGET_* (Bitget engine), NOT BYBIT_*.
    env = _load()["services"]["bot-usdc"].get("environment", [])
    joined = "\n".join(env)
    assert "BITGET_API_KEY=" in joined
    assert "BITGET_API_SECRET=" in joined
    assert "BITGET_API_PASSPHRASE=" in joined
    # the USDC (Bitget) service must NOT carry the Bybit override env that the OLD
    # Bybit-on-USDC config used (that would feed the wrong venue's keys).
    assert "BYBIT_API_KEY=" not in joined
    assert "BYBIT_API_SECRET=" not in joined


def test_bot_usd1_stays_on_bybit_credentials_unchanged():
    # zero-change: the USD1 ``bot`` service has no per-service key override (reads .env
    # BYBIT_* directly), exactly as before — it must NOT have grown BITGET_* env.
    env = _load()["services"]["bot"].get("environment", [])
    joined = "\n".join(env)
    assert "BITGET_API_KEY=" not in joined
