# Fixed Arithmetic Spot Grid Backtest Design

## Objective

Build a clean-room backtest of a conventional fixed arithmetic spot grid and
compare it with the current production slice-ladder, buy-and-hold, and idle
USDT. The result must cover USD1/USDT on Bybit and USDC/USDT on Bitget, use the
same market windows and capital per comparison, and separate parameter
selection from out-of-sample evaluation.

This experiment must not reuse the existing grid variants under `experiments/`
or use their results as evidence.

## Strategy Definition

The grid is fixed for the entire evaluation window:

- The initial reference is the first tradable bar open, rounded to the venue
  tick.
- Arithmetic price levels are placed at a constant `step_bp` around that
  reference, bounded by a symmetric `half_width_bp`.
- The reference, bounds, and levels never move or recenter.
- Adjacent levels define independent grid cells. If there are `n` levels on
  each side, capital is divided into `2n` equal cell budgets. The `n` cells
  below the reference start in quote and wait to buy at their lower boundary;
  the `n` cells above the reference start in base and wait to sell at their
  upper boundary. Initial base quantity is purchased at the effective first-bar
  acquisition price. Any residual caused by tick or quantity rounding remains
  idle quote.
- Every level below the reference starts with one resting buy. Every level above
  the reference starts with one resting sell.
- A buy fill creates a replacement sell exactly one grid step above its fill
  level. A sell fill creates a replacement buy exactly one grid step below its
  fill level.
- An order created by a fill becomes eligible only on the next bar. This forbids
  same-bar round trips whose ordering cannot be recovered from OHLC data.
- Multiple independent orders that existed at bar open may fill in the same bar.
- If one bar reaches eligible orders on both sides, the engine evaluates a
  buy-side-only branch and a sell-side-only branch from the same opening state,
  then keeps the branch with lower marked equity at that bar's close. A tie
  keeps the branch with fewer fills, then the buy-side branch. This deliberately
  refuses to invent an intrabar reversal path and cannot create opposing
  replacement orders at one price.
- When price leaves the grid, the grid does not chase it. Inventory saturates
  naturally toward base below the range and quote above the range. All holdings
  remain marked to market until the end.
- There is no leverage, borrowing, short selling, taker fallback, stop loss, or
  reinvestment of settled interest.

## Accounting And Fill Semantics

Each run starts with the configured capital: USD1/USDT uses USD 10,000 and
USDC/USDT uses USD 1,000.

Venue fees are zero, matching the configured zero-fee pairs. Every fill applies
the same adverse-selection haircut used by the existing backtest: buys pay
`limit * (1 + adv_bp / 10000)` and sells receive
`limit * (1 - adv_bp / 10000)`.

Two fill modes are reported:

- `touch`: an existing buy fills when the bar opens below it or its low reaches
  it; an existing sell fills when the bar opens above it or its high reaches it.
- `strict`: the bar must open beyond the order or trade strictly through it.

A 20% turnover gate is also applied to the conservative result. The aggregate
notional filled on one bar may not exceed 20% of that bar's turnover. Within the
selected side, marketable-at-open orders are considered first, followed by
orders in ascending absolute distance from the bar open; price and cell index
break ties. An order that does not fit in the remaining capacity is rejected for
that bar rather than partially filled. This ordering is deterministic and will
be pinned by tests.

USD1 carry uses the production `DailyMinInterest` rule at 6% APR. USDC and USDT
earn zero. Interest is computed from base inventory observed at hourly
boundaries, settled only for complete UTC days, and never reinvested.

Final equity is quote cash plus base inventory marked at the final close plus
settled interest. Initial inventory acquisition is accounted for at the first
open with the selected adverse-selection haircut; it is not a free conversion.

## Data And Evaluation Windows

The experiment uses public spot candles and records the venue in every result:

- USD1/USDT: Bybit spot 5-minute candles.
- USDC/USDT: Bitget spot 5-minute candles, matching the live venue.

Public candles will be refreshed through the latest complete bar needed for the
live-period comparison without overwriting unrelated user files. Data quality
checks cover monotonic timestamps, duplicate timestamps, missing OHLC values,
non-positive prices, bar interval gaps, and turnover availability.

For each symbol, the longest common clean historical window is split
chronologically into equal in-sample and out-of-sample halves. The in-sample
half alone selects parameters. Candidate values are:

- `step_bp`: 1, 2, 3, 5, 8.
- `half_width_bp`: 5, 10, 20, 40, 80.

Invalid combinations that cannot form at least one level on each side are
discarded. Selection maximizes the minimum total APR across adverse selection
`{0, 0.5, 1.0, 1.5}` under strict fills and the 20% turnover gate. Ties prefer,
in order, lower maximum drawdown, fewer fills, then wider grid spacing. No OOS
or live-period result may influence selection.

The selected parameters are then frozen and evaluated on:

1. The untouched historical OOS half.
2. The USD1 live run interval derived from persisted live status/state.
3. The USDC live run interval derived from persisted live status/state.

If a live interval begins after the historical training data, it remains a
forward test. The grid reference and inventory are initialized at that
interval's first available bar; parameters remain those selected earlier.

## Comparison Arms

Every evaluation window reports four independently accounted arms:

1. Fixed arithmetic grid.
2. Current configured slice-ladder, run from the same first bar with the same
   capital, symbol-specific parameters, carry, fill mode, adverse selection, and
   liquidity gate.
3. Buy-and-hold base, initialized at the same first bar and marked to market.
4. Idle USDT, fixed at initial capital.

The grid's native initial base/quote mix is part of the strategy and is not
silently granted to another arm. Results must report both total return and the
initial base allocation so the carry opportunity cost is visible.

## Outputs

The command produces a machine-readable JSON artifact plus a concise terminal
table. Each row includes:

- symbol, venue, window, dates, capital, bars, and data-quality summary;
- selected step, width, grid level count, and initial base allocation;
- total return, annualized return, price PnL, settled carry, and final equity;
- maximum drawdown, fills, completed grid cycles, turnover, and fill capacity
  rejections;
- time-weighted base/quote allocation, time outside the grid, and terminal
  saturation side;
- fill mode, liquidity gate, and adverse-selection assumption.

The final comparison emphasizes OOS and live-period strict results. In-sample
best performance is labeled selection evidence, not strategy evidence.

## Verification

Unit tests must prove:

- arithmetic levels and equal cell sizing are correct after tick rounding;
- initial inventory and cash conservation hold;
- a buy creates only the adjacent sell and a sell creates only the adjacent buy;
- replacements cannot fill on their creation bar;
- multiple pre-existing orders can fill without exceeding inventory or cash;
- price outside the range saturates inventory without recentering;
- touch and strict behavior differ on exact touches;
- adverse selection monotonically reduces equity on a controlled fixture;
- the liquidity gate is aggregate per bar and deterministic;
- USD1 daily-min carry reacts to hourly inventory dips;
- future-bar mutation cannot change earlier equity or parameter selection;
- result metrics reconcile to the final marked-to-market ledger.

Focused tests run before the full suite. Because this concerns money and
backtest validity, the finished implementation also passes the repository's
`codex-qa-gate` review before conclusions are reported.

## Non-Goals

- No production strategy or live order engine changes.
- No claim that candle fills reproduce queue position or maker profitability.
- No dynamic grid, recentering, trailing range, leverage, or parameter tuning on
  OOS/live results.
- No promotion to real-money trading based on this backtest alone.
