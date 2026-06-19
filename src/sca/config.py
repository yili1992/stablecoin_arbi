"""Central configuration + filesystem paths for stablecoin_arbi.

Everything tunable lives in ``config/strategy.yaml``; this module loads it once
and exposes ``CFG`` plus resolved paths so any module can do::

    from sca.config import CFG, DATA_DIR
"""
from __future__ import annotations
import os
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

# src/sca/config.py -> parents[2] == repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("SCA_DATA_DIR", REPO_ROOT / "data"))
CONFIG_PATH = Path(os.environ.get("SCA_CONFIG", REPO_ROOT / "config" / "strategy.yaml"))


def load_config(path: str | os.PathLike | None = None) -> dict:
    p = Path(path) if path else CONFIG_PATH
    if yaml is None:
        raise RuntimeError("pyyaml not installed — `pip install pyyaml`")
    with open(p) as f:
        return yaml.safe_load(f) or {}


# Loaded eagerly so modules can read constants at import time.
CFG: dict = load_config() if (yaml is not None and CONFIG_PATH.exists()) else {}


# ---------------------------------------------------------------------------
# Consolidated runtime resolvers — config/strategy.yaml is the single source.
# Precedence is always: env override  >  runtime: block  >  caller fallback.
# (Secrets are NOT resolved here — they stay env-only; see sca.live.creds.)
# ---------------------------------------------------------------------------
_RUNTIME_DEFAULTS = {"symbol": "USD1USDT", "seconds": 604800,
                     "mode": "paper", "dashboard_port": 3015}


def runtime(cfg: dict | None = None) -> dict:
    """Resolved launch defaults from the ``runtime:`` block, with baked-in
    fallbacks. ``cfg`` injectable for tests (defaults to the loaded ``CFG``)."""
    rt = (CFG if cfg is None else cfg).get("runtime", {})
    return {
        "symbol": rt.get("symbol", _RUNTIME_DEFAULTS["symbol"]),
        "seconds": int(rt.get("seconds", _RUNTIME_DEFAULTS["seconds"])),
        "mode": rt.get("mode", _RUNTIME_DEFAULTS["mode"]),
        "dashboard_port": int(rt.get("dashboard_port", _RUNTIME_DEFAULTS["dashboard_port"])),
    }


def out_dir(fallback: str = ".", cfg: dict | None = None) -> str:
    """Where status/csv/state files go: ``env SCA_OUT_DIR`` > ``runtime.out_dir``
    (only if set) > caller ``fallback``. Per-caller fallback preserved so defaults
    never shift (no orphaned bare-metal state). ``cfg`` injectable for tests."""
    env = os.environ.get("SCA_OUT_DIR")
    if env:
        return env
    rt_out = (CFG if cfg is None else cfg).get("runtime", {}).get("out_dir")
    return rt_out if rt_out else fallback


def resolve_mode(cfg: dict | None = None, env: dict | None = None) -> str:
    """Engine mode: ``env MODE`` > ``runtime.mode`` > ``"paper"``. Any unknown
    value coerces to ``"paper"`` — a typo'd MODE must never accidentally arm live.
    (The live gate still independently requires mode==live AND confirm AND keys.)"""
    env = os.environ if env is None else env
    m = env.get("MODE") or runtime(cfg)["mode"] or "paper"
    return m if m in ("paper", "live") else "paper"


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _as_bool(v) -> bool | None:
    """Parse a config/env truthiness value. Returns None for "unset/unknown" so a
    resolver can fall through to the next precedence tier (env value of ``""`` and
    unrecognized strings count as unset, never as an accidental True)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None


def resolve_testnet(cfg: dict | None = None, env: dict | None = None) -> bool:
    """SINGLE testnet source (F13): ``env BYBIT_TESTNET`` > ``runtime.testnet`` >
    ``live.testnet`` (DEPRECATED redirect) > ``False``.

    ``live.testnet`` is redirected here so old configs still resolve, but
    ``runtime.testnet`` is canonical and wins — the R1 read-client and the maker
    order client read this one resolver, so they can never be on different venues
    (no split-brain). Default ``False`` (mainnet); the maker client independently
    refuses to construct unless this is True, so 3a stays testnet-only."""
    env = os.environ if env is None else env
    ev = _as_bool(env.get("BYBIT_TESTNET"))
    if ev is not None:
        return ev
    c = CFG if cfg is None else cfg
    rt = c.get("runtime", {})
    if "testnet" in rt:
        return bool(rt.get("testnet"))
    live = c.get("live", {})
    if "testnet" in live:          # deprecated location, redirected through this resolver
        return bool(live.get("testnet"))
    return False


def resolve_maker_enabled(cfg: dict | None = None, env: dict | None = None) -> bool:
    """Maker order-path rollback knob (C-P1#14): ``env MAKER_ENABLED`` >
    ``runtime.maker_enabled`` > ``False``. Off => engine reverts to the paper
    ``evaluate_fills`` path with zero behavior change."""
    env = os.environ if env is None else env
    ev = _as_bool(env.get("MAKER_ENABLED"))
    if ev is not None:
        return ev
    rt = (CFG if cfg is None else cfg).get("runtime", {})
    return bool(rt.get("maker_enabled", False))
