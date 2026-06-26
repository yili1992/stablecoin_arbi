"""Single source of truth for Bybit credential env-var NAMES and their values.

Both Bybit clients — the read-only ``BybitPrivateClient`` and the live
``MakerOrderClient`` — resolve their API key/secret through here, so they can
never diverge onto different env vars (Codex P1 — credential env-name drift).

The env-var *names* are configurable via the ``live:`` block of
``config/strategy.yaml`` (``api_key_env`` / ``api_secret_env``); the legacy
hardcoded names are the defaults, so existing deployments are unaffected.
(D17 dropped the ``confirm_env`` name: ``LIVE_TRADING_CONFIRM`` stopped being a
gate in D14, so resolving it was dead bookkeeping — ``MODE=live`` is the one switch.)

Both inputs are injectable (``live_cfg``, ``env``) purely so this is trivially
unit-testable with plain dicts — production callers pass neither and get the real
config + ``os.environ``.
"""
from __future__ import annotations

import os

from sca.config import CFG

# Legacy hardcoded names — kept as defaults for full back-compat.
_DEFAULTS = {
    "api_key_env": "BYBIT_API_KEY",
    "api_secret_env": "BYBIT_API_SECRET",
    # OKX-family passphrase (Bitget). Bybit has no passphrase, so a Bybit deploy
    # never sets this name and resolve_passphrase() returns None for it.
    "api_passphrase_env": "BITGET_API_PASSPHRASE",
}


def credential_env_names(live_cfg: dict | None = None) -> tuple[str, str]:
    """Return (key_name, secret_name) from config, defaulting to the legacy
    hardcoded names when a field is absent."""
    cfg = CFG.get("live", {}) if live_cfg is None else live_cfg
    return (
        cfg.get("api_key_env", _DEFAULTS["api_key_env"]),
        cfg.get("api_secret_env", _DEFAULTS["api_secret_env"]),
    )


def resolve(live_cfg: dict | None = None, env: dict | None = None):
    """Return (key, secret) VALUES read from the env-var names in config.

    ``env`` defaults to ``os.environ``; ``live_cfg`` defaults to ``CFG['live']``.
    Missing values come back as ``None`` (never raises) — callers decide policy.
    """
    env = os.environ if env is None else env
    kn, sn = credential_env_names(live_cfg)
    return env.get(kn), env.get(sn)


def passphrase_env_name(live_cfg: dict | None = None) -> str:
    """Env-var NAME for the OKX-family passphrase (Bitget), from config, defaulting
    to the hardcoded ``BITGET_API_PASSPHRASE``. Kept SEPARATE from
    ``credential_env_names`` so the Bybit (key, secret) pair contract is unchanged."""
    cfg = CFG.get("live", {}) if live_cfg is None else live_cfg
    return cfg.get("api_passphrase_env", _DEFAULTS["api_passphrase_env"])


def resolve_passphrase(live_cfg: dict | None = None, env: dict | None = None):
    """Return the passphrase VALUE (Bitget) read from the configured env name, or
    ``None`` when unset (a Bybit deploy never sets it). Never raises."""
    env = os.environ if env is None else env
    return env.get(passphrase_env_name(live_cfg))
