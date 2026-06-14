# Onboarding — stablecoin_arbi

## What this is
Research + tooling for **0-fee stablecoin buy-low-sell-high on Bybit**. Bottom line: the
**yield is the real return**; the trading edge is ~0 out-of-sample. The one deciding unknown —
**live adverse selection** — is measured by `tools/dryrun.py` on a server.

## Architecture (boros-style)
```
config/strategy.yaml   single source of truth for all params (read via sca.config.CFG)
src/sca/
  ├── config.py        loads yaml + resolves paths
  ├── cli.py           `sca <cmd>` dispatcher
  ├── data/            fetch.py (Bybit klines) + loader
  ├── backtest/        engine.py (original strategy, loses to hold) · strategy.py (recommended)
  ├── optimize/        sweep.py (param sweep, IS/OOS)
  └── tools/           dryrun.py (live adverse-selection measurement)
scripts/run.py         run any command WITHOUT installing
tests/                 smoke tests (encode the findings as invariants)
docs/                  FINDINGS · STRATEGY · METHODOLOGY · conventions · decisions
data/ experiments/ reference/   klines · multi-agent search evidence · original freqtrade strategy
Dockerfile · docker-compose.yml (profiles: dryrun, tools) · docker-entrypoint.sh
```

## Quick start
```bash
pip install -e .                       # or: python scripts/run.py <cmd>   (no install)
sca backtest                           # recommended slice-ladder strategy
sca engine                             # original Freqtrade strategy (shows it loses to hold)
sca sweep                              # parameter sweep with out-of-sample validation
PYTHONPATH=src python -m pytest tests/ # smoke tests
```

### On your server — measure live fill quality
```bash
docker compose --profile dryrun up -d          # starts dryrun + dashboard → http://<host>:3005
docker compose --profile dryrun logs -f        # watch live markout; CSV in ./out
```

## The one rule
**Evidence before assertions.** Any "beats hold" must survive out-of-sample + an independent
from-scratch re-implementation (that is how the flashy 11.9% PAAL variant was killed).
Read `docs/FINDINGS.md` first, then `docs/METHODOLOGY.md`.
