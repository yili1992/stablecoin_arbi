# Strategy — EMA-anchored slice ladder (`strategy.py`, variant "r1_6")

The one design that survived adversarial verification and (thinly, in-sample) beats holding USD1.

## Idea in one sentence
Stay long USD1 (collect the 10% UTA carry ~97.5% of the time) and skim the recurring +5..+20 bp
mean-reverting spikes above a **floating** 1h-EMA anchor with a 5-slice sell ladder, re-buying
1 bp below the same floating anchor.

## Mechanics
Capital is split into **5 slices**, each independent:

| slice | NAV fraction | sell rung (above anchor) | rebuy |
|---|---|---|---|
| 1 | 15% | +5 bp | anchor − 1 bp |
| 2 | 18% | +7 bp | anchor − 1 bp |
| 3 | 20% | +10 bp | anchor − 1 bp |
| 4 | 22% | +14 bp | anchor − 1 bp |
| 5 | 25% | +20 bp | anchor − 1 bp |

- **anchor = EMA21 on the 1h timeframe** (floats with the market; uses only *closed* 1h candles → no lookahead).
- A slice in USD1 sells when price reaches its rung; a slice in USDT rebuys when price falls to anchor − 1 bp.
- **It is NOT all-in/all-out.** The fraction of capital momentarily in USDT equals how far price is
  above the anchor: ≤ anchor → ~0% in USDT; +5 bp → 15%; +10 bp → ~53%; +20 bp+ → up to 100%
  (a great price to have sold at). Most of the time price hugs the anchor, so idle-USDT averages **2.46%**.
- No stop-loss; relies on the (user-locked) assumption that USD1 always re-pegs.

## Why it beats the original (which loses)
The original re-buys at a *fixed* peg level, so after selling it sits idle in 0-yield USDT waiting
for a dip that may not come (idle-USDT high → carry drag → loses to hold). The **floating** anchor
re-buys near wherever price is now, so capital re-deploys into USD1 within ~2 days → idle-USDT 2.46%
→ almost no carry forfeited.

## Backtest (USD1USDT, ~6.7 months, \$10k, 10% carry)
| fill model | adv0 | adv0.5 | adv1.0 | adv1.5 |
|---|---|---|---|---|
| engine "touch" | 11.19 | 10.95 | 10.71 | 10.48 |
| strict trade-through | 10.54 | 10.38 | 10.21 | 10.05 |
| strict + 20% vol gate | 10.56 | 10.42 | 10.28 | 10.15 |

Price-only edge vs hold (interest off): +0.89 (touch) / ~+0.45 (strict) @adv0.5 → positive, so it
is real trading alpha, not an interest mirage. Capacity: \$1,248/day turnover at \$10k = 0.05% of
the \$2.5M/day market → scales to ~\$400k. MDD −0.53%.

## Parameter sweep (`sweep.py`) — what slice count / fractions actually do
- Full-sample APR is highest for back-loaded fractions + a wide rung range (5–30 bp), but the
  differences are small (all configs ~10.5–11%) and **in-sample**.
- **Out-of-sample: no configuration beats holding** (best OOS 10.58% vs 10.62% hold). Slice count
  from 1 to 10 barely changes return; more slices mainly smooths variance and idle time.
- Lesson: do not optimize for max in-sample APR — that is exactly how the rejected "PAAL" variant
  manufactured a fake 11.9% win. Optimize for out-of-sample robustness, and there is no edge to find.

## Caveats
1. Edge is thin (+0.2–0.4%/yr over hold) and **regime-dependent** (concentrated in the choppy
   first half; the last ~4 months had almost no tradeable spikes).
2. The true edge size depends on **live adverse selection** (unknown until measured with
   `tools/dryrun.py`).
3. n≈68 trades, single window. 0-fee is promotional.
