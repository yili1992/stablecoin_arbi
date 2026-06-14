# stablecoin_arbi

Research + tooling for **0-fee stablecoin "buy-low-sell-high" on Bybit spot**, structured like
[`boros_strategy`](https://github.com/yili1992/boros_strategy): all params in `config/strategy.yaml`,
a `src/sca/` package, a CLI, and a Docker stack with profiles.

> **TL;DR — read before trading anything.**
> 1. **Plain taker stat-arb is dead.** The per-trade spread (1–2 bp, tick-size floor) is *larger*
>    than the price oscillation (std ~0.7 bp). Zero fees don't fix that.
> 2. **The yield is the real return.** Held in Bybit's UTA, USD1 ≈ 10%, USDe/USDtb ≈ 3.5%;
>    USDT/USDC = 0%.
> 3. **The original Freqtrade strategy LOSES to holding USD1** (9.2–9.5% vs ~10%) — it parks
>    capital in 0-yield USDT.
> 4. **The redesigned slice-ladder only *thinly* beats holding** (+0.2–0.4%/yr) by keeping idle-USDT
>    to ~2.5% — and **no parameter config beats buy-and-hold out-of-sample.**
> 5. **The one deciding number — live adverse selection — can't be measured from candles.**
>    Run `dryrun` on a server. **Rational default: just hold USD1 for ~10%.**

## Architecture

```
config/strategy.yaml      ← single source of truth for ALL params (read via sca.config.CFG)
src/sca/
  ├── config.py           load yaml + resolve paths (SCA_CONFIG / SCA_DATA_DIR / SCA_OUT_DIR)
  ├── cli.py              `sca <command>` dispatcher
  ├── data/fetch.py       fetch Bybit klines → data/
  ├── backtest/engine.py  faithful backtest of the ORIGINAL Freqtrade strategy (loses to hold)
  ├── backtest/strategy.py the RECOMMENDED EMA-anchored slice-ladder (variant r1_6)
  ├── optimize/sweep.py   parameter sweep with in-sample / out-of-sample validation
  ├── live/engine.py      PAPER-trading engine: simulates the slice-ladder on LIVE Bybit data
  │                       (no orders, no key); a gated `live` mode is scaffolded. Writes
  │                       out/status_<symbol>.json (positions/indicators/PnL/klines/fills)
  ├── tools/dryrun.py     live adverse-selection measurement (Bybit WS, no orders, no key)
  └── tools/dashboard.py  zero-dependency web dashboard (中文 + candlestick) reading status JSON
scripts/run.py            run any command WITHOUT installing
tests/                    smoke tests (encode the findings as invariants)
docs/                     FINDINGS · STRATEGY · METHODOLOGY · conventions · decisions
data/  experiments/  reference/   klines · multi-agent search evidence · original freqtrade strategy
Dockerfile · docker-compose.yml (default: paper + dashboard; profiles: tools) · docker-entrypoint.sh · .env.example
```

## Quick start

```bash
pip install -e .                          # or use scripts/run.py below (no install)

sca paper         # PAPER-trade the slice-ladder on LIVE Bybit data (no orders/keys)
sca backtest      # recommended slice-ladder strategy + APR table
sca engine        # the ORIGINAL strategy (shows it loses to hold)
sca sweep         # parameter sweep with out-of-sample validation
sca fetch         # refresh data/ from Bybit
sca dashboard     # 中文 web dashboard (candlestick) for the paper engine's status JSON

# without installing:
python scripts/run.py backtest
PYTHONPATH=src python -m pytest tests/    # smoke tests
```

## Configuration

Everything tunable is in **`config/strategy.yaml`** (universe, slice ladder, backtest knobs,
sweep ranges, dryrun target). Change params there — not in code. Paths can be overridden with
`SCA_CONFIG`, `SCA_DATA_DIR`, `SCA_OUT_DIR`. No secrets are needed for backtests or dryrun;
see `.env.example`.

## Docker — run the paper engine + dashboard on your server

```bash
# ── the main thing to run: PAPER-trade the slice-ladder on LIVE data ──
docker compose up -d --build                          # starts paper + dashboard (SYMBOL/SECONDS via env/.env)
docker compose logs -f paper                          # watch fills + live markout summaries
#   → 中文 dashboard:  http://<host>:3015   (positions · indicators · candlestick · PnL · fills)
#   → engine writes ./out/status_<symbol>.json   ·   CSV + per-boot logs in ./out/

# ── offline tools (one-shot) ──
docker compose --profile tools run --rm backtest
docker compose --profile tools run --rm engine
docker compose --profile tools run --rm sweep
docker compose --profile tools run --rm fetch
```

**Paper is the default — it places NO real orders and needs NO API key.** It simulates fills off
the live Bybit order book using the *exact* slice-ladder rules from the backtest, so paper == backtest.

### Going live (gated — real money, your own risk)
The `live` mode is scaffolded but **refuses to trade by accident**. It places real orders ONLY when
**all** of these hold, else it errors out:
1. `MODE=live` (or `sca live ... --mode live`)
2. `LIVE_TRADING_CONFIRM=yes`
3. `BYBIT_API_KEY` + `BYBIT_API_SECRET` present

Set them in `.env` (see `.env.example`) and `docker compose up -d --build` again. A hard per-order
notional cap lives in `config/strategy.yaml` (`live.max_order_usd`). Watch the paper dashboard first.

**Fill quality is the real edge gauge:** the `ROUND-TRIP` markout (bp) is your per-trade edge (maps to
the backtest's `adv`). `>0` net → a real edge exists; `≈0`/negative → the strategy ≈ holding (or worse).
Run for **days**, ideally spanning active periods. This strategy only *thinly* beats holding in-sample
and **not** out-of-sample — the dashboard does not imply guaranteed profit.

## Key results (USD1USDT, ~6.7 months, $10k, 10% UTA carry)

| Strategy | adv=0 | **adv=0.5 bp** | adv=1.0 bp | vs hold (~10.27%) |
|---|---|---|---|---|
| Original Freqtrade (`sca engine`) | 11.07 | 9.57 | 9.25 | **loses** |
| Slice-ladder, touch fills (`sca backtest`) | 11.19 | 10.95 | 10.71 | +0.7% |
| Slice-ladder, strict + 20% liquidity gate | 10.56 | 10.42 | 10.28 | **+0.15%** |
| **Hold USD1 (do nothing)** | — | **~10.27** | — | benchmark |

**Out-of-sample (`sca sweep`):** across every slice count / fraction / rung config, the best OOS
APR is **10.58% vs 10.62% for holding** — *nothing beats holding out-of-sample.* The in-sample
"wins" are regime-fitting.

## Caveats
- Single ~6.7-month window, ~68 trades; 0-fee is **promotional** (edge dies if fees revert).
- No stop-loss; downside bounded by buy-and-hold **only if USD1 always re-pegs** (cf. UST→$0, now USTC ≈ $0.006).
- Backtest fills are upper bounds; the deciding variable is live adverse selection → run `dryrun`.

Full analysis: [`docs/FINDINGS.md`](docs/FINDINGS.md) · [`docs/STRATEGY.md`](docs/STRATEGY.md) ·
[`docs/METHODOLOGY.md`](docs/METHODOLOGY.md). Conventions/decisions: [`docs/conventions.md`](docs/conventions.md) ·
[`docs/decisions.md`](docs/decisions.md).
