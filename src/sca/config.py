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
