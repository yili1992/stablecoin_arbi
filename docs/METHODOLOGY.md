# Methodology — how the backtests avoid lying to you

Stablecoin spreads are ~1 bp, so a sloppy backtest will manufacture phantom edge. The guards here.

## No lookahead
- 1h EMAs are usable only **after** the 1h candle closes: an informative row opening at H is merged
  onto 5m bars with ts ≥ H + 3600 s (`merge_asof` on a shifted `avail_ts`). A 5m decision at bar i
  uses the open price + the most recent *already-closed* 1h EMA — never future data.
- Resting limit orders are evaluated against the bar's own low/high; the order level is known at bar open.

## Fill models (pick your realism)
- **touch** (engine default, optimistic): a passive buy fills if `low ≤ limit`, a sell if `high ≥ limit`.
  Assumes front-of-queue, full size. Upper bound.
- **strict trade-through**: requires the bar to *trade through* the level (`open` past it or the
  extreme strictly beyond), modelling that a mere touch may not clear the queue ahead of you.
- **liquidity gate** (`liq_gate=f`): a fill is allowed only if `order_notional ≤ f · bar_turnover`.
  This is the test that killed the overfit variants — they "filled" \$10k orders on \$4–\$200 bars.

## Adverse selection (`adv`)
The dominant unknown. Modelled as a per-fill haircut: a buy fills at `limit·(1 + adv/1e4)`, a sell at
`limit·(1 − adv/1e4)`. It is a markout stress-test, swept over {0, 0.5, 1.0, 1.5} bp/side. **It is not
faithful to live queue dynamics** — only `tools/dryrun.py` measures the real value (see below).

## Interest accrual
10% (USD1) APR accrues per 5m bar **only on capital currently in USD1**, into a separate bucket that
is **not** reinvested (conservative). USDT/USDC = 0%.

## Benchmark
The honest bar is **realized buy-and-hold USD1 marked in USDT** (deploy all capital at the first open
with one adverse haircut, hold, accrue interest) — ~10.27% on this window, vs the LOCKED flat-10% bar.
The +0.27% is one-time re-peg drift any holder captures.

## In-sample / out-of-sample
`sweep.py` splits the window in half, optimizes on the first half, validates on the second, and ranks
by `min(IS, OOS)` — not full-sample max. This is what exposes regime-fitting.

## The Freqtrade fidelity trap (why we don't trust its own backtest)
The original strategy gates entry/exit via `has_open_trade` written into the dataframe from
`custom_info`. In Freqtrade backtests, `populate_indicators` runs once up front, so that flag is a
constant (0) broadcast over the whole column → entry fires every bar, the exit signal never fires →
the backtest bypasses the real `confirm_trade_exit` logic. It behaves completely differently live.
Also: `custom_exit_price` returning `None` does **not** mean "no order" — Freqtrade falls back to
`proposed_rate`. These are why we re-implemented everything as a transparent event loop.

## Live fill-quality measurement (`tools/dryrun.py`)
Candles cannot reveal adverse selection. The tool streams Bybit's live trades + order book and, each
time a trade hits the bid (a passive buy would fill) or ask (a passive sell would fill), records the
mid-price drift over the next 5 s / 30 s — the **markout**, signed so positive = maker profit. The
round-trip markout (buy + sell) in bp is the real per-trade edge, and maps directly to the `adv` knob.
Zero orders, no API key. Run for days on infra near the exchange. (Account-specific queue/latency would
require real tiny orders — intentionally not included.)

## Adversarial verification
Every candidate that claimed to beat the benchmark was independently **re-implemented from scratch** by
a separate agent (`experiments/bt_verify_*.py`) that hunted for lookahead, fill optimism, interest
double-counting, capacity violations, and overfitting (first-half vs second-half stability). A claim
survived only if the from-scratch reimplementation reproduced it AND no material issue was found.
