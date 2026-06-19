"""Tests for the single credential resolver (sca.live.creds) — Phase 1, T1/P1.0.

WHY this exists (Codex P1): the read-only ``BybitPrivateClient`` and the live
``MakerOrderClient`` must resolve their API key/secret from the SAME env-var names,
or they diverge (read one credential set, trade with another). The config exposes
configurable *names* (``live.api_key_env`` / ``live.api_secret_env``); this module
is the single source of truth for those NAMES + their resolved values. (D14 removed
the ``LIVE_TRADING_CONFIRM`` gate; D17 dropped its name too — credentials are now a
(key, secret) PAIR, not a 3-tuple.)

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
    # D17: the confirm env-var name was dropped (D14 already removed it as a gate), so
    # this is a (key_name, secret_name) PAIR, not the old 3-tuple.
    kn, sn = creds.credential_env_names({})
    assert (kn, sn) == ("BYBIT_API_KEY", "BYBIT_API_SECRET")


def test_custom_env_names_are_honored():
    live_cfg = {"api_key_env": "X_KEY", "api_secret_env": "X_SECRET"}
    kn, sn = creds.credential_env_names(live_cfg)
    assert (kn, sn) == ("X_KEY", "X_SECRET")


def test_resolve_reads_values_under_configured_names():
    live_cfg = {"api_key_env": "X_KEY", "api_secret_env": "X_SECRET"}
    env = {"X_KEY": "abc", "X_SECRET": "def", "BYBIT_API_KEY": "WRONG"}
    key, secret = creds.resolve(live_cfg=live_cfg, env=env)
    # must read the *configured* name, never the legacy default when a custom name is set
    assert (key, secret) == ("abc", "def")


def test_resolve_missing_values_returns_none_pair():
    key, secret = creds.resolve(live_cfg={}, env={})
    assert key is None and secret is None


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
