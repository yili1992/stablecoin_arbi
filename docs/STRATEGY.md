# Strategy — EMA-anchored floor/rest slice-ladder canary (`strategy.py`)

The current production configuration is a live-measurement probe for fill rate, queue loss, and
markout. It is not a claim that trading beats simply holding USD1.

## Idea in one sentence
Stay long USD1 for carry, post low sell rungs often enough to measure real maker execution, protect
normal sells with an entry-cost floor, and surrender after a deeper anchor break so the probe can
reset cost instead of freezing forever.

## Mechanics
Capital is split into **5 slices**, each independent:

| slice | NAV fraction | sell rung | rebuy |
|---|---|---|---|
| 1 | 15% | +1 bp over rule-Z base | anchor − 1 bp |
| 2 | 18% | +2 bp over rule-Z base | anchor − 1 bp |
| 3 | 20% | +3 bp over rule-Z base | anchor − 1 bp |
| 4 | 22% | +4 bp over rule-Z base | anchor − 1 bp |
| 5 | 25% | +5 bp over rule-Z base | anchor − 1 bp |

- **anchor = EMA21 on the 1h timeframe** (floats with the market; uses only *closed* 1h candles → no lookahead).
- A slice in USD1 sells at `max(anchor, entry_cost + min_profit_bp) + rung`; `min_profit_bp=1` in
  the canary config.
- If `anchor < entry_cost - rest_bps` (`rest_bps=15`), the slice surrenders at `anchor + rung`.
- A slice in USDT rebuys when price falls to anchor − 1 bp; that rebuy price becomes the next
  tracked entry cost.
- No stop-loss; relies on the (user-locked) assumption that USD1 always re-pegs.

## Why it is safer than the original (which loses)
The original re-buys at a *fixed* peg level, so after selling it sits idle in 0-yield USDT waiting
for a dip that may not come (idle-USDT high → carry drag → loses to hold). The **floating** anchor
re-buys near wherever price is now, and the floor/rest rule avoids routine loss sells while allowing
a controlled reset after a deeper anchor break.

## Backtest (USD1USDT, ~6.7 months, \$10k, 10% carry)
| fill model | adv0 | adv0.5 | adv1.0 | adv1.5 |
|---|---|---|---|---|
| engine "touch" | 10.81 | 9.99 | 9.50 | 9.04 |
| strict + 20% vol gate | 9.20 | 8.92 | 8.77 | 8.65 |

Price-only edge vs hold (interest off): +1.16 touch @adv0.5 on the full window, but total APR still
trails realized hold because USDT dwell loses carry. At adv0.5 the touch model produces about
1.1 sell fills/day on the full window, enough to measure markout.

## Parameter sweep (`sweep.py`) — what slice count / fractions actually do
Older no-floor sweeps found that no rung/fraction configuration beat holding out of sample. The
current floor/rest probe changes the objective: it is tuned for canary sample collection while
remaining close to holding, not for declaring an optimized edge.

## Caveats
1. Edge is unproven; the current config trails realized hold at adv0.5+ in backtest.
2. The true edge size depends on **live adverse selection** (unknown until measured with
   `tools/dryrun.py`).
3. n≈68 trades, single window. 0-fee is promotional.
