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


# --- passphrase (Bitget / OKX-family) ---------------------------------------
# Bitget needs a passphrase in addition to key/secret. It is a SEPARATE resolver
# so the Bybit (key, secret) PAIR contract above is unchanged. Default env name is
# BITGET_API_PASSPHRASE; configurable via live.api_passphrase_env. A venue without a
# passphrase (Bybit) simply has no such name configured -> resolves to None.

def test_passphrase_default_env_name():
    assert creds.passphrase_env_name({}) == "BITGET_API_PASSPHRASE"


def test_passphrase_custom_env_name_honored():
    assert creds.passphrase_env_name({"api_passphrase_env": "X_PASS"}) == "X_PASS"


def test_resolve_passphrase_reads_configured_name():
    live_cfg = {"api_passphrase_env": "X_PASS"}
    env = {"X_PASS": "phrase", "BITGET_API_PASSPHRASE": "WRONG"}
    assert creds.resolve_passphrase(live_cfg=live_cfg, env=env) == "phrase"


def test_resolve_passphrase_missing_returns_none():
    assert creds.resolve_passphrase(live_cfg={}, env={}) is None


# --- per-exchange credential routing (Phase 3) ------------------------------
# A single config now serves two venues at once (USD1@Bybit + USDC@Bitget). The
# resolver routes default env-var NAMES by exchange so each engine reads its own
# venue's keys. feedback_shared_mapping_no_duplicate: ONE resolver, exchange in →
# correct names out; callers don't hardcode names. Bybit (default) stays a 2-tuple.

def test_credential_env_names_bybit_default_unchanged():
    # default exchange (omitted) AND explicit "bybit" both yield the legacy Bybit names —
    # zero-change guarantee for the existing USD1 deploy.
    assert creds.credential_env_names({}) == ("BYBIT_API_KEY", "BYBIT_API_SECRET")
    assert creds.credential_env_names({}, exchange="bybit") == (
        "BYBIT_API_KEY", "BYBIT_API_SECRET")


def test_credential_env_names_bitget_routes_to_bitget_names():
    assert creds.credential_env_names({}, exchange="bitget") == (
        "BITGET_API_KEY", "BITGET_API_SECRET")


def test_resolve_bitget_reads_bitget_env_names():
    env = {"BITGET_API_KEY": "bg-k", "BITGET_API_SECRET": "bg-s",
           "BYBIT_API_KEY": "by-k", "BYBIT_API_SECRET": "by-s"}
    key, secret = creds.resolve(live_cfg={}, env=env, exchange="bitget")
    assert (key, secret) == ("bg-k", "bg-s")          # NOT the Bybit values


def test_resolve_bybit_reads_bybit_env_names_when_both_present():
    env = {"BITGET_API_KEY": "bg-k", "BITGET_API_SECRET": "bg-s",
           "BYBIT_API_KEY": "by-k", "BYBIT_API_SECRET": "by-s"}
    key, secret = creds.resolve(live_cfg={}, env=env, exchange="bybit")
    assert (key, secret) == ("by-k", "by-s")


def test_passphrase_bitget_default_name_routed_by_exchange():
    assert creds.passphrase_env_name({}, exchange="bitget") == "BITGET_API_PASSPHRASE"


def test_passphrase_bybit_resolves_none_value():
    # Bybit has no passphrase; even if BITGET_API_PASSPHRASE is set in env, the bybit
    # route must NOT pick it up (the Bybit 2-tuple contract is unchanged).
    env = {"BITGET_API_PASSPHRASE": "should-not-leak"}
    assert creds.resolve_passphrase(live_cfg={}, env=env, exchange="bybit") is None


def test_resolve_passphrase_bitget_reads_value():
    env = {"BITGET_API_PASSPHRASE": "phrase"}
    assert creds.resolve_passphrase(live_cfg={}, env=env, exchange="bitget") == "phrase"


def test_custom_env_names_still_honored_for_default_exchange():
    # the live: block override still wins on the (default/bybit) path — back-compat.
    live_cfg = {"api_key_env": "X_KEY", "api_secret_env": "X_SECRET"}
    env = {"X_KEY": "abc", "X_SECRET": "def"}
    assert creds.resolve(live_cfg=live_cfg, env=env) == ("abc", "def")


def test_bybit_flat_key_override_does_NOT_leak_into_bitget():
    # PRODUCTION BUG GUARD: config/strategy.yaml's live: block sets api_key_env=BYBIT_API_KEY
    # (a Bybit-named field). That flat override must NOT bleed into the Bitget route — the
    # Bitget engine must still read BITGET_API_KEY/SECRET, or it would auth with Bybit names.
    live_cfg = {"api_key_env": "BYBIT_API_KEY", "api_secret_env": "BYBIT_API_SECRET"}
    assert creds.credential_env_names(live_cfg, exchange="bitget") == (
        "BITGET_API_KEY", "BITGET_API_SECRET")


def test_bybit_flat_key_override_still_applies_to_bybit_route():
    # the flat api_key_env/api_secret_env name Bybit's creds, so a custom Bybit name is
    # still honored on the explicit bybit route (configurable-name feature unbroken).
    live_cfg = {"api_key_env": "CUSTOM_BYBIT_KEY", "api_secret_env": "CUSTOM_BYBIT_SECRET"}
    assert creds.credential_env_names(live_cfg, exchange="bybit") == (
        "CUSTOM_BYBIT_KEY", "CUSTOM_BYBIT_SECRET")


def test_passphrase_flat_override_does_NOT_leak_into_bybit():
    # api_passphrase_env names BITGET's passphrase; it must never produce a passphrase on
    # the bybit route (Bybit has none) even if the field is set in a shared config.
    live_cfg = {"api_passphrase_env": "SOME_PASS"}
    assert creds.passphrase_env_name(live_cfg, exchange="bybit") is None


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
