#!/usr/bin/env python3
"""Run any sca command WITHOUT installing the package (no `pip install -e .` needed):

    python scripts/run.py backtest
    python scripts/run.py sweep
    python scripts/run.py dryrun --symbol USD1USDT --seconds 86400
    python scripts/run.py fetch --days 210

(If you `pip install -e .`, use the `sca <command>` entry point instead.)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from sca.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
