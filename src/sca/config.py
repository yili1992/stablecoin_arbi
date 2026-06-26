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
                     "mode": "dryrun", "status_every": 60, "summary_every": 60,
                     "dashboard_port": 3015}
_STRATEGY_DEFAULTS = {"min_profit_bp": 0.0, "rest_bps": 0.0}


def runtime(cfg: dict | None = None) -> dict:
    """Resolved launch defaults from the ``runtime:`` block, with baked-in
    fallbacks. ``cfg`` injectable for tests (defaults to the loaded ``CFG``)."""
    rt = (CFG if cfg is None else cfg).get("runtime", {})
    return {
        "symbol": rt.get("symbol", _RUNTIME_DEFAULTS["symbol"]),
        "seconds": int(rt.get("seconds", _RUNTIME_DEFAULTS["seconds"])),
        "mode": rt.get("mode", _RUNTIME_DEFAULTS["mode"]),
        "status_every": int(rt.get("status_every", _RUNTIME_DEFAULTS["status_every"])),
        "summary_every": int(rt.get("summary_every", _RUNTIME_DEFAULTS["summary_every"])),
        "dashboard_port": int(rt.get("dashboard_port", _RUNTIME_DEFAULTS["dashboard_port"])),
    }


def strategy(cfg: dict | None = None) -> dict:
    """Resolved strategy knobs with backward-compatible defaults."""
    st = (CFG if cfg is None else cfg).get("strategy", {})
    return {
        "min_profit_bp": float(st.get("min_profit_bp", _STRATEGY_DEFAULTS["min_profit_bp"])),
        "rest_bps": float(st.get("rest_bps", _STRATEGY_DEFAULTS["rest_bps"])),
    }


# Full per-symbol strategy parameter set; defaults mirror the engine/backtest module
# fallbacks so a symbol with no override behaves exactly as before.
_STRATEGY_PARAM_DEFAULTS: dict = {
    "rungs": [5, 7, 10, 14, 20],
    "fractions": [0.15, 0.18, 0.20, 0.22, 0.25],
    "min_profit_bp": 0.0,
    "rest_bps": 0.0,
    "anchor_ema_span": 21,
    "rebuy_offset_bp": -1.0,
    "interest_apr": 0.10,
}


def strategy_for(symbol: str, cfg: dict | None = None) -> dict:
    """Effective strategy params for one ``symbol`` = the default ``strategy`` block
    overlaid with that symbol's ``universe[].strategy`` override (dict-merge).

    A symbol with no override returns the defaults unchanged (USD1's zero-change
    regression guarantee). ``cfg`` injectable for tests. Fails fast on a malformed
    ladder (fractions must sum to 1 and match rungs length) so a bad live config
    can never silently mis-size an order."""
    c = CFG if cfg is None else cfg
    merged = dict(c.get("strategy", {}) or {})
    for u in c.get("universe", []) or []:
        if u.get("symbol") == symbol:
            merged.update(u.get("strategy", {}) or {})
            break
    out = {k: merged.get(k, d) for k, d in _STRATEGY_PARAM_DEFAULTS.items()}
    assert abs(sum(out["fractions"]) - 1.0) < 1e-9, \
        f"{symbol}: fractions sum {sum(out['fractions'])} != 1"
    assert len(out["rungs"]) == len(out["fractions"]), \
        f"{symbol}: rungs({len(out['rungs'])})/fractions({len(out['fractions'])}) length mismatch"
    return out


def exchange_for(symbol: str, cfg: dict | None = None) -> str:
    """Effective exchange id for one ``symbol`` = ``universe[symbol].exchange``,
    defaulting to ``"bybit"`` for any symbol without the field (every current
    symbol is still bybit — zero-change guarantee). ``cfg`` injectable for tests."""
    c = CFG if cfg is None else cfg
    for u in c.get("universe", []) or []:
        if u.get("symbol") == symbol:
            return u.get("exchange", "bybit")
    return "bybit"


def max_alloc_for(symbol: str, cfg: dict | None = None) -> float:
    """Effective deployment cap (USD) for one ``symbol`` = ``universe[symbol].
    max_total_alloc_usd`` if set (explicit, INCL. 0 = deploy nothing), else the
    global ``live.max_total_alloc_usd`` fallback, else -1 (no cap = full wallet).
    The ONLY real-money fund limit on a spot account (capital deployed = loss
    ceiling). ``cfg`` injectable for tests. Mirrors ``exchange_for`` (universe is a
    list; a present-but-None field falls through to the global, an explicit 0 does
    NOT — 0 is an intentional 'deploy nothing')."""
    c = CFG if cfg is None else cfg
    for u in c.get("universe", []) or []:
        if u.get("symbol") == symbol:
            v = u.get("max_total_alloc_usd")
            if v is not None:
                return float(v)
            break
    return float(c.get("live", {}).get("max_total_alloc_usd", -1.0))


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
    """Engine mode (D14 — two modes only): ``env MODE`` > ``runtime.mode`` >
    ``"dryrun"``. Any unknown value coerces to ``"dryrun"`` — the SAFE default — so a
    typo'd MODE can never accidentally select the real-money ``live`` path.

      * ``dryrun`` — run the maker engine but SIMULATE matching (no order client, no
        keys, no real orders); the markout gauge still records adverse selection.
      * ``live``   — place real GTC maker orders on mainnet (real money). ``MODE=live``
        is the ONE switch; missing API keys raise naturally at client construction."""
    env = os.environ if env is None else env
    m = env.get("MODE") or runtime(cfg)["mode"] or "dryrun"
    return m if m in ("dryrun", "live") else "dryrun"
