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
> 4. **The current slice-ladder is a canary probe, not a carry strategy.** With
>    floor=1bp/rest=15bp it stays close to holding while creating fills, but it still
>    trails realized buy-and-hold once adverse selection is non-zero.
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
  ├── backtest/strategy.py EMA-anchored floor/rest slice-ladder canary probe
  ├── optimize/sweep.py   parameter sweep with in-sample / out-of-sample validation
  ├── live/engine.py      dryrun/live engine: simulates or places PostOnly slice-ladder orders
  │                       depending on runtime.mode. Writes
  │                       out/status_<symbol>.json (positions/indicators/PnL/klines/fills)
  ├── tools/dryrun.py     live adverse-selection measurement (Bybit WS, no orders, no key)
  └── tools/dashboard.py  zero-dependency web dashboard (中文 + candlestick) reading status JSON
scripts/run.py            run any command WITHOUT installing
tests/                    smoke tests (encode the findings as invariants)
docs/                     FINDINGS · STRATEGY · METHODOLOGY · conventions · decisions
data/  experiments/  reference/   klines · multi-agent search evidence · original freqtrade strategy
Dockerfile · docker-compose.yml (default: bot + dashboard; profiles: tools) · docker-entrypoint.sh · .env.example
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
docker compose up -d --build                          # starts bot + dashboard (symbol/seconds/mode in config/strategy.yaml `runtime:`)
docker compose logs -f bot                            # watch fills + live markout summaries
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

### Going live (real money, MAINNET, your own risk)
`live` mode places real GTC PostOnly maker orders on **mainnet**. Two modes only (D14):
- `dryrun` (default) — simulates fills, places no real orders, needs no API key.
- `live` — `MODE=live` **alone** = real money (no extra confirm env). It needs `BYBIT_API_KEY` +
  `BYBIT_API_SECRET`; a missing key raises a clear error (it never trades un-keyed).

Set the keys in `.env` (see `.env.example`) and select live mode — `MODE=live` in `.env` or
`runtime.mode: live` in `config/strategy.yaml` (env `MODE` wins) — then start the single engine
service: `docker compose up -d --build bot`. There is ONE engine service; the mode controls
dryrun-vs-live, with no separate service or profile (D17). The only fund limit is
`config/strategy.yaml` `live.max_total_alloc_usd` (capital deployed = loss cap on a spot account;
`-1` = whole wallet). **Docker live is safe (D16):** the `bot` service runs `restart: on-failure`,
so a transient crash self-heals, but an operator-reconcile halt persists across restarts and a
resumed-halted engine refuses to continue with a clean exit (0) — the bot stays stopped until you
reset it (delete `./out/<symbol>_live_state.json` for a fresh start, or set `LIVE_CLEAR_HALT=yes`
to clear the halt but keep the position). Bare-metal `sca live` also works. Watch the dryrun
dashboard first.

**Fill quality is the real edge gauge:** the `ROUND-TRIP` markout (bp) is your per-trade edge (maps to
the backtest's `adv`). `>0` net → a real edge exists; `≈0`/negative → the strategy ≈ holding (or worse).
Run for **days**, ideally spanning active periods. The current low-floor config is meant to collect
live fill / queue-loss data; the dashboard does not imply guaranteed profit.

## Key results (USD1USDT, ~6.7 months, $10k, 10% UTA carry)

| Strategy | adv=0 | **adv=0.5 bp** | adv=1.0 bp | vs hold (~10.27%) |
|---|---|---|---|---|
| Original Freqtrade (`sca engine`) | 11.07 | 9.57 | 9.25 | **loses** |
| Floor/rest probe, touch fills (`sca backtest`) | 10.81 | 9.99 | 9.50 | **loses at adv0.5+** |
| Floor/rest probe, strict + 20% liquidity gate | 9.20 | 8.92 | 8.77 | **loses** |
| **Hold USD1 (do nothing)** | ~10.26 | **~10.25** | ~10.24 | benchmark |

**Out-of-sample:** the probe is useful for measurement, not proof of edge: at adv=0 touch OOS is
~10.65% vs ~10.56% hold, but at adv=0.5 it drops to ~9.93% vs ~10.54% hold. The unknown that matters
is live fill rate / queue loss / markout.

## Caveats
- Single ~6.7-month window; 0-fee is **promotional** (edge dies if fees revert).
- No stop-loss; downside bounded by buy-and-hold **only if USD1 always re-pegs** (cf. UST→$0, now USTC ≈ $0.006).
- Backtest fills are upper bounds; the deciding variable is live adverse selection → run `dryrun`.

Full analysis: [`docs/FINDINGS.md`](docs/FINDINGS.md) · [`docs/STRATEGY.md`](docs/STRATEGY.md) ·
[`docs/METHODOLOGY.md`](docs/METHODOLOGY.md). Conventions/decisions: [`docs/conventions.md`](docs/conventions.md) ·
[`docs/decisions.md`](docs/decisions.md).
