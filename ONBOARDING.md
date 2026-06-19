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
  ├── live/            engine.py (PAPER-trade slice-ladder on LIVE Bybit data; gated live mode)
  └── tools/           dryrun.py (adverse-selection measurement) · dashboard.py (中文 + candlestick)
scripts/run.py         run any command WITHOUT installing
tests/                 smoke tests (encode the findings as invariants)
docs/                  FINDINGS · STRATEGY · METHODOLOGY · conventions · decisions
data/ experiments/ reference/   klines · multi-agent search evidence · original freqtrade strategy
Dockerfile · docker-compose.yml (default: paper + dashboard; profiles: tools) · docker-entrypoint.sh
```

## Quick start
```bash
pip install -e .                       # or: python scripts/run.py <cmd>   (no install)
sca paper                              # PAPER-trade the slice-ladder on LIVE Bybit data (no orders/keys)
sca backtest                           # recommended slice-ladder strategy
sca engine                             # original Freqtrade strategy (shows it loses to hold)
sca sweep                              # parameter sweep with out-of-sample validation
sca dashboard                          # 中文 web dashboard (candlestick) for the paper engine
PYTHONPATH=src python -m pytest tests/ # smoke tests
```

### On your server — run the paper engine + dashboard
```bash
docker compose up -d --build                   # starts paper + dashboard → http://<host>:3015
docker compose logs -f paper                   # watch fills + markout; status JSON + CSV in ./out
```
Dryrun (the default) places **no real orders** and needs **no API key**. Going live (real money,
mainnet) is just `MODE=live` + `BYBIT_API_KEY`/`BYBIT_API_SECRET` in `.env` (see `.env.example`) —
`MODE=live` alone = real money (D14); a missing key raises. The dashboard shows positions,
indicators (floating EMA anchor,
sell rungs, rebuy line), a candlestick chart, PnL, and fill quality — it does **not** imply profit.

## The one rule
**Evidence before assertions.** Any "beats hold" must survive out-of-sample + an independent
from-scratch re-implementation (that is how the flashy 11.9% PAAL variant was killed).
Read `docs/FINDINGS.md` first, then `docs/METHODOLOGY.md`.
