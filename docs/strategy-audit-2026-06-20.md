# Pre-Live Strategy Audit — 2026-06-20

Run before the first real-money canary, via two checklists: `arb-strategy-check` and
`mr-strategy-check`. **No code bug found; this is a review record, not a code change.**
Both audits converge on the same honest conclusion already in `FINDINGS.md`.

## Strategy classification (read this first)

This is a **single-venue, single-leg, spot maker mean-reversion ladder** on USD1USDT
(anchor = 21-span EMA on closed 1h candles; sell rungs at `anchor+[1..5]bp`, rebuy at
`anchor−1bp`). It is **NOT** a paired-leg / delta-neutral / cross-exchange arbitrage.

Consequence: most of the arb checklist is **N/A** — no second leg (no delta-neutral, no
two-leg atomicity / naked-leg risk), single venue (no cross-exchange matching / settlement).
The **mean-reversion** checklist fits better and bites harder.

## First-principles verdict (the convergent conclusion)

| Principle | Verdict |
|:--|:--|
| Cost/markout is the life-or-death line (arb #2) | **THE open question, by design.** For a maker the "cost" is adverse selection (markout), not taker spread. Net = captured spread − markout. Price-edge ≈ 0 OOS. The live `markout` gauge (5s/30s horizons) is the verdict. Honest instrumentation; economics unproven until the canary. |
| Loosening ≠ improvement; low-freq is correct; real frequency needs a new signal, not tuning (MR #1) | **Applies.** Normal-regime price std 0.4–0.7bp **< the 1–2bp** you must cross → few profitable round-trips is **math, not a bug**. Do NOT tighten rungs / loosen the anchor to "trade more" (less captured per trip, same markout → worse EV). The real lever — floating anchor compressing idle-USDT to **2.46%** — is already done. |
| IS-Sharpe has ~0 OOS predictive power; metric-ranked selection = overfit (MR #2) | **PASS.** only-USD1 is **carry-driven** (USD1 ~10% UTA interest; USDC spread-leg EV≈0), not IS-Sharpe ranked. `sweep.py` does a proper IS/OOS split + `robust=min(IS,OOS)` + hold benchmark. |
| Universe expansion / re-TPE is usually a negative thesis; current params often already OOS-optimal (MR #3) | **Applies.** Sweep: OOS no config beats hold; slice count barely matters. Re-sweeping rungs/span for OOS gains = overfitting noise. Adding USDe/USDtb = the universe-expansion trap (more depeg tail; USD1 has the best carry). Don't. |

## Findings (priority order)

1. **Maker profitability is UNVALIDATED — by design, not a bug.** Price-edge ≈ 0 OOS
   (`FINDINGS`: out-of-sample *nothing beats holding USD1 ~10%*). Whether the ladder beats
   just-holding is unproven; the canary's markout answers it. The code is honest — it measures
   adverse selection (`engine.py:aggregate_markout`), does not fabricate edge.

2. **By the MR go-live bar, the price-MR does NOT qualify as a live alpha.** The MR checklist's
   own rule: *no clean OOS edge → don't take MR live as alpha.* The price-MR here has no clean
   OOS support. The canary is defensible **only** because (a) the real return is **carry** (you
   hold USD1 → the recommended default anyway), (b) downside is bounded by the capital cap, and
   (c) no-stop + cap (not force-flatten) is the correct posture for unvalidated MR (MR-B5).
   It is **"hold USD1 for carry + measure whether the maker adds anything"**, NOT proof of alpha.

3. **The live order path has NEVER hit real Bybit** (backtest = optimistic touch-fills, explicitly
   "front-of-queue, not real"; dryrun simulates off the live book but places no orders / uses no
   key). So the **first $1000 live run is simultaneously the integration test** (auth, PostOnly
   reject, WS reconnect, partial fills, cancel-to-terminal vs real exchange responses) **and** the
   economics test. Treat the first minutes as a gray-launch — watch order placement/fills/halt.

4. **Directional long-USD1 (not delta-neutral).** Tail = USD1 depeg. Same risk as the recommended
   "hold USD1" baseline; capital cap bounds it; no-stop is deliberate. Not a new risk.

5. **Capacity unmeasured at scale** (forward-looking). $1000 markout/fill-rate ≠ $10k+. Re-measure
   before scaling.

## PASS (execution machinery — heavily reviewed: 3-persona + Codex×5 + qa in 3a; persona+qa after)

Order lifecycle (partial-fill three-state / cancel-to-terminal / orderLinkId idempotency /
ambiguous→unattributed→operator-halt, don't-act-on-uncertainty), durable operator-halt across
restart (D16), R1 reconcile gate, per-mode data separation (state/events/status mode-tagged),
capital cap enforced in the real sizing path, **default dryrun** (typo/unknown → dryrun, never
accidental real money), NaN/precision floor-rounding. Backtest: no-lookahead (closed candles),
IS/OOS done, same data source as live (Bybit spot — no yfinance-style contamination).

## Canary success criteria — 3 numbers decide it

1. **markout (5s/30s) sign & magnitude** vs the 1–5bp captured spread. If markout eats the spread →
   maker is **negative-EV** (= FINDINGS' OOS conclusion confirmed live → revert to plainly holding USD1).
2. **Fill rate / round-trip frequency.** Too low = mathematical necessity (std < spread), not a bug.
3. **Integration health** (first minutes): order-placement success, PostOnly reject rate, WS
   disconnects, whether the halt mis-fires.

## Bottom line

Machinery is live-ready and reviewed. **Economics (does the maker earn?) is unknown — the canary
measures it, bounded by the $1000 cap.** `FINDINGS` already says nothing beats holding OOS; the
canary gives that conclusion one chance to be refuted live — it is not an attempt to prove an edge
exists. If markout eats the spread (likely), the correct action is to revert to holding USD1, **not**
to re-tune parameters.
