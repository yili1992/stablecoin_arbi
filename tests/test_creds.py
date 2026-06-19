"""Tests for the single credential resolver (sca.live.creds) — Phase 1, T1/P1.0.

WHY this exists (Codex P1): ``engine.live_authorization()`` historically hardcoded
``BYBIT_API_KEY`` / ``BYBIT_API_SECRET`` / ``LIVE_TRADING_CONFIRM`` while the config
exposes configurable *names* (``live.api_key_env`` etc.). If the new ccxt client
reads the config names but the arm-check reads hardcoded ones, the two diverge
(arm one credential set, trade with another). This module is the single source of
truth for credential env-var NAMES + their resolved values.

ISOLATION: pure unit tests — both ``live_cfg`` and ``env`` are injected as plain
dicts; nothing touches the real process environment or config file.

Run: PYTHONPATH=src python3 -m pytest tests/test_creds.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live import creds  # noqa: E402


def test_default_env_names_match_legacy_hardcoded():
    # back-compat: an empty live cfg yields exactly the legacy hardcoded names.
    cn, kn, sn = creds.credential_env_names({})
    assert (cn, kn, sn) == ("LIVE_TRADING_CONFIRM", "BYBIT_API_KEY", "BYBIT_API_SECRET")


def test_custom_env_names_are_honored():
    live_cfg = {"confirm_env": "X_CONFIRM", "api_key_env": "X_KEY", "api_secret_env": "X_SECRET"}
    cn, kn, sn = creds.credential_env_names(live_cfg)
    assert (cn, kn, sn) == ("X_CONFIRM", "X_KEY", "X_SECRET")


def test_resolve_reads_values_under_configured_names():
    live_cfg = {"confirm_env": "X_CONFIRM", "api_key_env": "X_KEY", "api_secret_env": "X_SECRET"}
    env = {"X_CONFIRM": "yes", "X_KEY": "abc", "X_SECRET": "def", "BYBIT_API_KEY": "WRONG"}
    confirm, key, secret = creds.resolve(live_cfg=live_cfg, env=env)
    # must read the *configured* name, never the legacy default when a custom name is set
    assert (confirm, key, secret) == ("yes", "abc", "def")


def test_resolve_missing_values_returns_none_triplet():
    confirm, key, secret = creds.resolve(live_cfg={}, env={})
    assert confirm is None and key is None and secret is None


# --- engine.live_authorization: armed iff mode==live (D14) ---
# (credential_env_names/resolve above remain the single source of truth for the ORDER
#  client's key/secret; live_authorization itself no longer reads creds — MODE=live is
#  the ONE switch and missing keys raise at client construction, see test_orders.py.)

def test_live_authorization_armed_in_live_mode():
    from sca.live import engine
    armed, reason = engine.live_authorization("live")
    assert armed is True, reason                       # mode=live alone arms (no confirm env)


def test_live_authorization_dryrun_mode_never_armed():
    from sca.live import engine
    # non-live mode is never armed, regardless of env (dryrun = simulated, no real orders)
    armed, _ = engine.live_authorization("dryrun")
    assert armed is False
