# Findings — the full research narrative

The honest end-to-end conclusion of investigating 0-fee stablecoin buy-low-sell-high on Bybit.

## 0. The question
Bybit waives spot fees on several stablecoin pairs that hover around \$1 and wobble a few bp.
Is there enough spread to arbitrage? And which other 0-fee stables exist?

## 1. Market structure (live Bybit API)
- 0-fee, ~\$1 pegged spot pairs: **USDC/USDT, USD1/USDT, USDe/USDT, USDtb/USDT** (named), plus
  **RLUSD/USDT, PYUSD/USDT, USDe/USDC, USDS/USDT** (found). (User later confirmed RLUSD/USDS are
  *not* 0-fee → dropped.)
- **tickSize = 0.0001 = 1 bp on every pair → that is the hard floor on the bid-ask spread.**
- **Normal-regime price std ≈ 0.4–0.7 bp, i.e. *smaller* than the 1–2 bp spread you must cross.**
  Even with 0 fees, a taker mean-reversion trade's expected capture (`|z|·σ`) is below the cost
  threshold → **plain taker stat-arb is mathematically a non-starter.**
- Order books on the thin pairs (USDe, USDtb, PYUSD) are lopsided (e.g. USDtb \$12 bid vs \$4.9M
  ask) → the quoted 1–2 bp spread is illusory beyond a few \$k.

## 2. The yield is the real engine (not the price game)
Held in Bybit's Unified Trading Account, the coins earn interest: **USD1 ≈ 10%**, USDe ≈ 3.5%,
USDtb ≈ 3.5%. **USDT and USDC held earn 0%.** This reframes everything: the goal is to stay in a
yield-bearing coin, and the enemy is sitting in USDT.

## 3. The original Freqtrade strategy loses to holding
The user's `ArbiStrategy` (buy ≤ EMA55(1h), sell at +1–2 bp, hold ≥2 days for interest, no stop)
backtested faithfully (`backtest_engine.py`):

| adv (bp/side) | 0 | 0.5 | 1.0 | 1.5 |
|---|---|---|---|---|
| Total APR | 11.07 | 9.57 | 9.25 | 8.82 |

It **loses to simply holding USD1 (~10%)** at any realistic adverse selection, because selling
to USDT forfeits ~7–10% of the time in 0-yield USDT — a carry drag the ~0 price edge can't repay.
(Codex independently reproduced these numbers.)

## 4. The current production probe: EMA-anchored floor/rest slice ladder (`strategy.py`)
Keep capital home in USD1 (collect 10%), use low sell rungs as a live fill-quality probe, and protect
normal exits with a cost floor: sell from `max(anchor, entry_cost + 1bp) + rung`; if the anchor breaks
15bp below entry, surrender at `anchor + rung` so the next rebuy resets cost. This is designed to
generate enough maker fills to measure queue loss / markout, not to maximize carry.

| fill model | adv=0.5 | adv=1.0 |
|---|---|---|
| engine "touch" | 9.99 | 9.50 |
| strict + 20% liquidity gate | 8.92 | 8.77 |

At adv=0 the probe can show positive price-only OOS edge, but at adv=0.5+ it trails realized hold.
This is exactly why the canary exists: real adverse selection and queue loss cannot be inferred from
OHLCV.

## 5. Parameter optimization → no out-of-sample edge (`sweep.py`)
Swept slice count (1–10) × fraction shape (equal/front/back) × rung range, with an in-sample
(first half) / out-of-sample (second half) split:
- In-sample "max return" ≈ 11% — but it is **regime-fitting** to the choppy first half.
- **Out-of-sample, every config returns ≤ 10.58% vs 10.62% for holding → nothing beats holding
  OOS.** Slice count barely affects return; the second half has almost no tradeable spikes.

## 6. Heterogeneous review + adversarial verification
- **Codex (GPT)** confirmed the conclusion (est. 7–9.7%, won't underwrite >10%), and corrected two
  things: (a) Freqtrade `custom_exit_price → None` falls back to `proposed_rate` (sells underwater,
  so the sim was *too optimistic* there); (b) the honest bar is mark-to-market hold ≈ 10.27%, not
  flat 10%.
- An **ultracode workflow** generated 8 variants, backtested each, and **adversarially
  re-implemented every "beats-10%" claim from scratch.** It killed the flashy headlines: "PAAL"
  (11.93%) was overfit (all trades in one 2-month window; last 64% of the sample zero trades) and
  liquidity-infeasible (tried to sell \$10k into \$4–\$200-volume bars). The decisive test:
  *can the order size actually fill on the bar it claims to fill on?* Only the slice-ladder
  survived (its fills land on ~\$14k-volume bars).

## 7. Bottom line
- Plain taker arb: **dead.**
- Trade-the-spread for yield-coins: the original design **loses** to holding; the current floor/rest
  ladder is a **measurement canary** that stays near hold while producing fills, not a proven
  long-term allocation.
- The repeatable edge is essentially the **yield**. **Just holding USD1 (~10%) is the rational
  default.**
- The one variable that could change this — **live adverse selection** — is unmeasurable from
  candles. Measure it with `tools/dryrun.py` before committing.
- The one potentially-bigger edge not tested here — **par redemption arbitrage** — needs off-chain
  redemption mechanics.
