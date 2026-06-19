"""CLI dispatcher — `sca <command> [args]`.

Mirrors boros's `cli/index.ts` command pattern. Each command runs the
corresponding module's ``__main__`` block via runpy (so their argparse/output
behave exactly as when run with `python -m sca.<module>`).
"""
from __future__ import annotations
import sys
import runpy

COMMANDS = {
    "backtest": "sca.backtest.strategy",   # recommended slice-ladder strategy backtest
    "engine":   "sca.backtest.engine",     # original Freqtrade strategy (baseline)
    "sweep":    "sca.optimize.sweep",      # parameter sweep w/ IS/OOS validation
    "fetch":    "sca.data.fetch",          # refresh kline data from Bybit
    "dryrun":   "sca.tools.dryrun",        # live adverse-selection measurement
    "dashboard":"sca.tools.dashboard",     # live web dashboard for dryrun results
    "paper":    "sca.live.engine",         # run the engine in the resolved mode (dryrun by default: no orders/keys)
    "live":     "sca.live.engine",         # same engine; injects --mode live (real money, MAINNET; needs API keys)
    "balance":  "sca.live.bybit_client",   # print Bybit UTA wallet balance (read-only; needs API key in env)
}


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: sca <" + "|".join(COMMANDS) + "> [args]")
        return
    cmd, rest = argv[0], argv[1:]
    if cmd not in COMMANDS:
        print(f"unknown command: {cmd!r}\nusage: sca <{'|'.join(COMMANDS)}> [args]")
        sys.exit(2)
    if cmd == "live" and "--mode" not in rest:
        rest = rest + ["--mode", "live"]   # `sca live` selects real-money MAINNET mode (needs API keys)
    sys.argv = [cmd] + rest          # so the module's argparse sees the right args
    runpy.run_module(COMMANDS[cmd], run_name="__main__")


if __name__ == "__main__":
    main()
