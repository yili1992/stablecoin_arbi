# stablecoin_arbi

Research into **0-fee stablecoin "buy-low-sell-high" on Bybit spot**, plus the tooling to
decide whether it is worth trading at all.

The headline question was: *Bybit waives fees on several stablecoin spot pairs (USD1, USDe,
USDtb, …) that wobble around \$1 — is there enough spread to arbitrage?* This repo contains
the full, honest answer: faithful backtests, a parameter optimizer with out-of-sample
validation, an adversarially-verified strategy search, and a live fill-quality measurement
tool.

> **TL;DR (read this before trading anything).**
> 1. **Plain taker "stat-arb" is dead.** On the liquid pairs the per-trade spread (~1–2 bp,
>    tick-size floor) is *larger* than the price oscillation (std ~0.7 bp). Zero fees do not
>    fix that.
> 2. **The only real return is the yield, not the price game.** Holding USD1 / USDe / USDtb in
>    Bybit's Unified Trading Account earns interest (USD1 ≈ 10%, USDe/USDtb ≈ 3.5%). USDT/USDC
>    held earn 0%.
> 3. **The original Freqtrade strategy LOSES to just holding USD1** (9.2–9.5% vs ~10%) because
>    it parks capital in 0-yield USDT waiting for dips.
> 4. **A redesigned slice-ladder (`strategy.py`) only *thinly* beats holding** (+0.2–0.4%/yr)
>    by keeping idle-USDT time to ~2.5% — and **even that edge disappears out-of-sample**:
>    no parameter configuration beats buy-and-hold in the second half of the data.
> 5. **The single number that decides everything — live adverse selection — cannot be measured
>    from candles.** Run `tools/dryrun.py` on a server near the exchange to measure it. That is
>    the one open question.
>
> **Rational default: just hold USD1 for the ~10% carry, unless your live fills prove
> unusually good.**

---

## Repo layout

```
strategy.py            The recommended strategy: EMA-anchored slice-ladder (variant "r1_6"),
                       standalone (non-Freqtrade). Backtest + reference implementation.
backtest_engine.py     Faithful event-driven backtest of the ORIGINAL Freqtrade strategy,
                       with an adverse-selection sweep + UTA-interest accrual.
sweep.py               Parameter optimizer: slice count x per-slice fraction x rung range,
                       with IN-SAMPLE / OUT-OF-SAMPLE split (anti-overfit).
tools/dryrun.py        Live fill-quality (adverse-selection) measurement via Bybit WebSocket.
                       Zero orders, zero API key. >>> run this on your server. <<<
fetch_data.py          Regenerate the kline CSVs from Bybit's public API.
data/                  ~6.7 months of 5m + ~15 months of 1h klines (USD1/USDe/USDtb), public.
reference/             The original user Freqtrade strategy + the Codex review payload.
experiments/           The full strategy-search exploration (8 variants + adversarial
                       re-implementations). Evidence trail; see experiments/README below.
docs/                  FINDINGS, STRATEGY, METHODOLOGY — the detailed write-ups.
```

## Quickstart

```bash
pip install -r requirements.txt

python3 strategy.py          # the recommended strategy's backtest + APR table
python3 backtest_engine.py   # the ORIGINAL strategy (shows it loses to hold)
python3 sweep.py             # parameter sweep with out-of-sample validation
python3 fetch_data.py        # (optional) refresh data/ from Bybit
```

### The important one — measure live fill quality on your server

```bash
pip install websockets
python3 tools/dryrun.py --symbol USD1USDT --seconds 86400 --csv usd1_adv.csv
```

This streams Bybit's live trades + order book and, every time a passive top-of-book order
*would* have filled, measures how the mid-price drifts over the next 5 s / 30 s (the
"markout"). It places **no orders** and needs **no API key**. The output round-trip markout in
bp is your real per-trade edge — the knob that decides whether any of this beats holding.
Run it for **days**, ideally on infra near the exchange, spanning active periods (stablecoins
are flat most of the time; adverse selection only shows up during moves).

## Key results (USD1USDT, ~6.7 months, \$10k, 10% UTA carry)

| Strategy | adv=0 (perfect fills) | **adv=0.5 bp** | adv=1.0 bp | vs hold (~10.27%) |
|---|---|---|---|---|
| Original Freqtrade (`backtest_engine.py`) | 11.07 | 9.57 | 9.25 | **loses** |
| Slice-ladder, engine "touch" fills (`strategy.py`) | 11.19 | 10.95 | 10.71 | +0.7% |
| Slice-ladder, strict + 20% liquidity gate | 10.56 | 10.42 | 10.28 | **+0.15%** |
| **Hold USD1 (do nothing)** | — | **~10.27** | — | benchmark |

**Out-of-sample (parameter sweep, `sweep.py`):** across every slice count / fraction / rung
config, the best out-of-sample APR is **10.58% vs 10.62% for holding** — i.e. **nothing beats
holding out-of-sample.** The in-sample "wins" are regime-fitting to the choppy first half.

`adv` = adverse selection in bp per fill: how much worse your passive fill is than its quoted
price (you fill exactly when the market is moving against you). It is the dominant variable and
is **not measurable from OHLCV** — hence `tools/dryrun.py`.

## Honest caveats

- Single 6.7-month window, ~68 trades — not multi-regime, not statistically deep.
- 0-fee is a **promotion**, not permanent; if fees revert to 0.10% the whole thing dies instantly.
- No stop-loss; downside is bounded by buy-and-hold **only under the assumption USD1 always
  re-pegs**. A genuine permanent depeg (cf. UST → \$0, still on Bybit as USTC ≈ \$0.006) breaks
  that assumption.
- Backtest fills are upper bounds; `strategy.py` ships `touch` / `strict` / liquidity-gate
  models so you can see how fast the edge decays under realism.

## The one remaining real edge (untested here)

Codex (heterogeneous review) flagged **par-conversion / primary-redemption arbitrage** — buy
USD1 below \$1, redeem/convert at par — as the only design that does **not** depend on perfect
fills. It cannot be backtested from klines (needs redemption fees / limits / settlement /
eligibility). Worth chasing off-platform.

See [`docs/FINDINGS.md`](docs/FINDINGS.md), [`docs/STRATEGY.md`](docs/STRATEGY.md), and
[`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) for the full analysis.
