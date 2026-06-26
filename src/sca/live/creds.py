"""Single source of truth for exchange credential env-var NAMES and their values.

Both Bybit clients — the read-only ``BybitPrivateClient`` and the live
``MakerOrderClient`` — plus the adapter-driven ``PrivateReadClient`` resolve their
API key/secret (+ Bitget passphrase) through here, so they can never diverge onto
different env vars (Codex P1 — credential env-name drift).

PER-EXCHANGE routing (Phase 3): a single config now serves two venues at once
(USD1@Bybit + USDC@Bitget). Passing ``exchange="bitget"`` routes the DEFAULT env
names to ``BITGET_API_KEY/SECRET/PASSPHRASE``; ``exchange="bybit"`` (or the legacy
no-arg call) routes to ``BYBIT_API_KEY/SECRET`` with NO passphrase (Bybit has none).
feedback_shared_mapping_no_duplicate: ONE resolver, exchange in -> correct names out.

Back-compat (Bybit zero-change): omitting ``exchange`` reproduces the legacy behavior
exactly — Bybit (key, secret) pair, and the ``BITGET_API_PASSPHRASE`` passphrase name
(an un-set name simply resolves to None, as it always did for a Bybit-only deploy).
The env-var *names* are still overridable via the ``live:`` block of
``config/strategy.yaml`` (``api_key_env`` / ``api_secret_env`` / ``api_passphrase_env``);
the override wins when present.

Both inputs are injectable (``live_cfg``, ``env``) purely so this is trivially
unit-testable with plain dicts — production callers pass neither and get the real
config + ``os.environ``.
"""
from __future__ import annotations

import os

from sca.config import CFG

# Legacy hardcoded names — kept as defaults for full back-compat (no exchange arg).
_DEFAULTS = {
    "api_key_env": "BYBIT_API_KEY",
    "api_secret_env": "BYBIT_API_SECRET",
    # OKX-family passphrase (Bitget). Bybit has no passphrase, so a Bybit deploy
    # never sets this name and resolve_passphrase() returns None for it.
    "api_passphrase_env": "BITGET_API_PASSPHRASE",
}

# Per-exchange DEFAULT env-var names. ``passphrase`` is None for venues without one
# (Bybit) so resolve_passphrase short-circuits to None on that route.
_PER_EXCHANGE = {
    "bybit": {"key": "BYBIT_API_KEY", "secret": "BYBIT_API_SECRET", "passphrase": None},
    "bitget": {"key": "BITGET_API_KEY", "secret": "BITGET_API_SECRET",
               "passphrase": "BITGET_API_PASSPHRASE"},
}


def _exchange_defaults(exchange: str | None) -> dict:
    """Default name map for ``exchange``. ``None`` => the legacy global defaults
    (Bybit key/secret + the Bitget passphrase name) so a no-arg call is unchanged."""
    if exchange is None:
        return {"key": _DEFAULTS["api_key_env"], "secret": _DEFAULTS["api_secret_env"],
                "passphrase": _DEFAULTS["api_passphrase_env"]}
    try:
        return _PER_EXCHANGE[exchange]
    except KeyError:
        raise ValueError(f"unknown exchange {exchange!r} for credential routing")


def credential_env_names(live_cfg: dict | None = None,
                         exchange: str | None = None) -> tuple[str, str]:
    """Return (key_name, secret_name), routed by ``exchange`` (None => legacy Bybit).

    The legacy flat override (``live.api_key_env`` / ``api_secret_env``) names BYBIT's
    key/secret (its default is ``BYBIT_API_KEY``), so it applies ONLY to the default
    (None) / ``"bybit"`` route. It must NOT leak into another venue (``"bitget"`` reads
    BITGET_* from per-exchange defaults), or a shared config that sets it for Bybit would
    silently feed Bybit env names to the Bitget engine (Phase-3 production bug)."""
    cfg = CFG.get("live", {}) if live_cfg is None else live_cfg
    d = _exchange_defaults(exchange)
    if exchange in (None, "bybit"):
        return (cfg.get("api_key_env", d["key"]), cfg.get("api_secret_env", d["secret"]))
    return (d["key"], d["secret"])


def resolve(live_cfg: dict | None = None, env: dict | None = None,
            exchange: str | None = None):
    """Return (key, secret) VALUES read from the env-var names for ``exchange``.

    ``env`` defaults to ``os.environ``; ``live_cfg`` defaults to ``CFG['live']``.
    Missing values come back as ``None`` (never raises) — callers decide policy.
    """
    env = os.environ if env is None else env
    kn, sn = credential_env_names(live_cfg, exchange=exchange)
    return env.get(kn), env.get(sn)


def passphrase_env_name(live_cfg: dict | None = None,
                        exchange: str | None = None) -> str | None:
    """Env-var NAME for the OKX-family passphrase, routed by ``exchange`` (None => the
    legacy ``BITGET_API_PASSPHRASE`` default; ``"bybit"`` => None, no passphrase).

    The flat override (``live.api_passphrase_env``) names BITGET's passphrase, so it
    applies ONLY to the routes that HAVE a passphrase (None / ``"bitget"``). For
    ``"bybit"`` the name is unconditionally None — Bybit has no passphrase, and the flat
    field must never produce one there even if a shared config sets it. Kept SEPARATE from
    ``credential_env_names`` so the Bybit (key, secret) pair contract is unchanged."""
    default_name = _exchange_defaults(exchange)["passphrase"]
    if default_name is None:                 # venue has no passphrase (bybit)
        return None
    cfg = CFG.get("live", {}) if live_cfg is None else live_cfg
    return cfg.get("api_passphrase_env", default_name)


def resolve_passphrase(live_cfg: dict | None = None, env: dict | None = None,
                       exchange: str | None = None):
    """Return the passphrase VALUE (Bitget) read from the configured env name for
    ``exchange``, or ``None`` when unset / the venue has no passphrase (Bybit). Never
    raises."""
    name = passphrase_env_name(live_cfg, exchange=exchange)
    if name is None:                       # venue has no passphrase (Bybit)
        return None
    env = os.environ if env is None else env
    return env.get(name)
