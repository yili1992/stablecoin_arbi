"""Single source of truth for Bybit credential env-var NAMES and their values.

Both the live-order safety gate (``engine.live_authorization``) and the private
ccxt client (``BybitPrivateClient``) resolve credentials through here, so they can
never diverge onto different env vars (Codex P1 — credential env-name drift).

The env-var *names* are configurable via the ``live:`` block of
``config/strategy.yaml`` (``confirm_env`` / ``api_key_env`` / ``api_secret_env``);
the legacy hardcoded names are the defaults, so existing deployments are unaffected.

Both inputs are injectable (``live_cfg``, ``env``) purely so this is trivially
unit-testable with plain dicts — production callers pass neither and get the real
config + ``os.environ``.
"""
from __future__ import annotations

import os

from sca.config import CFG

# Legacy hardcoded names — kept as defaults for full back-compat.
_DEFAULTS = {
    "confirm_env": "LIVE_TRADING_CONFIRM",
    "api_key_env": "BYBIT_API_KEY",
    "api_secret_env": "BYBIT_API_SECRET",
}


def credential_env_names(live_cfg: dict | None = None) -> tuple[str, str, str]:
    """Return (confirm_name, key_name, secret_name) from config, defaulting to the
    legacy hardcoded names when a field is absent."""
    cfg = CFG.get("live", {}) if live_cfg is None else live_cfg
    return (
        cfg.get("confirm_env", _DEFAULTS["confirm_env"]),
        cfg.get("api_key_env", _DEFAULTS["api_key_env"]),
        cfg.get("api_secret_env", _DEFAULTS["api_secret_env"]),
    )


def resolve(live_cfg: dict | None = None, env: dict | None = None):
    """Return (confirm, key, secret) VALUES read from the env-var names in config.

    ``env`` defaults to ``os.environ``; ``live_cfg`` defaults to ``CFG['live']``.
    Missing values come back as ``None`` (never raises) — callers decide policy.
    """
    env = os.environ if env is None else env
    cn, kn, sn = credential_env_names(live_cfg)
    return env.get(cn), env.get(kn), env.get(sn)
