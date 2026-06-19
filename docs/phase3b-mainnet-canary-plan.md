# PHASE 3b — Mainnet Enablement + Fund Caps + Max-Loss Kill-Switch (small-cap canary)

**Status:** design + TDD plan, for Codex review then implementation. **Scope:** 3b only — a tight DELTA on the merged 3a maker layer. **DoD = merge-ready, NOT merged, NOT run.** The real-money canary RUN is the owner's separate action (keys + env confirms). testnet is dropped (owner decision: dryrun-or-live; dryrun already run).

> **Guiding principle (feedback_multi_mode_parity):** live and testnet share IDENTICAL strategy logic + execution flow; the ONLY difference is the executor/venue. So 3b is a **gate + config + sizing-cap + kill-switch** change. It does **NOT** touch the maker order-lifecycle logic (reconcile/poll/cancel-to-terminal/persistence) hardened in 3a.

> **Why each guard (检索门):** `arb-execution-risk` — a configured cap that the live sizing path fails to apply is a real defect → the total-alloc cap MUST be enforced in `_seed_slices_from_balance`, not just stored. `ai-quant-validation` production gate — real money needs a **kill-switch** (halt = flatten/stop). `trading-risk-control` — risk must be **pre-trade & atomic**, not post-trade-only.

---

## A. Mainnet opt-in — DUAL confirm, additive (testnet stays default) `(3b-1)`

3a hard-refuses mainnet in 3 places: `MakerOrderClient.__init__` (orders.py:84), `place_postonly` (orders.py:146), and `_compute_maker_enabled` requires `resolve_testnet()` (engine.py:1536). 3b lifts these **only behind a deliberate, separate second confirm** — never by flipping one flag.

- New `config.resolve_allow_mainnet(cfg, env)` (mirrors `resolve_testnet` precedence): true **iff** `runtime.allow_mainnet == true` (config, default **false**) **AND** env **`LIVE_MAINNET_CONFIRM == "yes"`** (a NEW env var, distinct from `LIVE_TRADING_CONFIRM`). Both required → mainnet allowed. Either missing → mainnet stays refused.
- `_compute_maker_enabled` becomes: `armed AND resolve_maker_enabled() AND (resolve_testnet() OR resolve_allow_mainnet())`. All existing 3a gates (mode=live, LIVE_TRADING_CONFIRM, keys present) STILL apply unchanged. Mainnet just swaps the `testnet` requirement for the stricter `allow_mainnet` dual-confirm.
- Client construction: when mainnet-allowed, build `MakerOrderClient`/`BybitPrivateClient` with `testnet=False` (live venue) instead of raising. The ctor/place mainnet refusal becomes: refuse on mainnet **unless `resolve_allow_mainnet()`**. So an un-opted-in mainnet still hard-raises (3a behavior preserved as the default).
- **`fresh_deploy` refusal stays UNCONDITIONAL** (engine.py — unchanged): mainnet first-position is still reached only via seed-from-balance → `proceed`, never an inferred fresh deploy.
- RED: `test_mainnet_refused_without_allow_mainnet`, `test_mainnet_refused_without_LIVE_MAINNET_CONFIRM_env`, `test_mainnet_allowed_with_dual_confirm`, `test_testnet_path_unchanged_default`, `test_resolve_allow_mainnet_precedence`.

## B. Total-alloc canary cap — enforced in sizing `(3b-2, the canary's core safety)`

The boss's "限制多少资金,-1 = 用钱包里所有的钱" maps to a **total deployment cap**, because `_seed_slices_from_balance` (engine.py:1046) currently seeds slices from the FULL real wallet balance → on a funded account it would deploy everything. For a $300 canary on a $50k wallet that is unacceptable.

- New `live.max_total_alloc_usd` (canary value, e.g. 300; **`-1` ⇒ no cap = use full available wallet**).
- In `_seed_slices_from_balance`: `deployable_quote = free_quote if cap < 0 else min(free_quote, cap)` (and the base side valued at mark for the cap comparison). Seed slices from **`deployable`**, not raw wallet. The cap is applied at the SINGLE sizing entry point (parity with the `arb-execution-risk` lesson — enforce in the live path, not just config).
- Interaction with `_available_from_balance` (eng:1335, used by reconcile sizing): the running available pool must also be bounded by the remaining alloc budget so re-quotes never exceed the canary cap.
- RED: `test_total_alloc_caps_deployment_below_wallet`, `test_total_alloc_minus1_uses_full_wallet`, `test_reconcile_respects_total_alloc_budget`.

## C. Per-order cap `-1` = unlimited `(3b-3, the boss's param)`

3a: `_clamp_to_cap` (order_recon:95) clamps `qty*px <= max_order_usd`; `place_postonly` HARD-ASSERTS it (orders.py:141, raises). 3b: `max_order_usd < 0` ⇒ **no per-order cap**.

- `_clamp_to_cap`: `if max_order_usd is None or max_order_usd < 0 or px <= 0: return qty` (no clamp).
- `place_postonly` hard-assert: skip when `self.max_order_usd < 0`.
- **Doc the danger inline + in decisions:** `-1` removes the last-line per-order guard against a garbage-size order — intended only AFTER the canary validates sizing; **do not use on run #1**.
- RED: `test_clamp_minus1_no_cap`, `test_place_postonly_minus1_skips_assert`, `test_finite_cap_still_enforced` (regression — positive cap still clamps + asserts).

## D. Max-loss kill-switch `(3b-4, real-money mandate)`

3a has NO loss-based halt (only operator-reconcile / persist / reject-streak halts). Real money needs one (production gate: halt = flatten/stop; reset = human root-cause).

- New `live.max_loss_usd` (canary e.g. 50; `0`/`-1` ⇒ disabled, but **default ON for canary**).
- Track session PnL = `realized_capture + unrealized` where unrealized = mark-to-market of open slices vs their entry/cost basis (reuse `_slice_value` / `start_value`). Compute drawdown = `start_value + realized + unrealized − start_value` ... i.e. `equity − start_equity`.
- In `maker_step` (after `poll_fills` books fills, before/with `reconcile_orders`): if `loss := start_equity − current_equity` and `loss >= max_loss_usd` (cap>0) → **`_halt`**: route through `_cancel_all_resting` (cancel-to-terminal each, the 3a-safe path) + set `_halted` + refuse further placement + loud log. Reuse the existing halt plumbing (`OperatorReconcileHalt`-style); reset requires restart + human (no auto-reset).
- Checked **pre-trade-ish** (each step, before new places), atomic with the step (per `trading-risk-control`).
- RED: `test_max_loss_halts_and_cancels_all`, `test_max_loss_disabled_when_zero`, `test_halt_blocks_further_placement`, `test_loss_just_under_threshold_no_halt` (boundary).

## E. Markout logging on the live path `(3b-5, the canary's PURPOSE)`

The markout/adverse-selection gauge already exists (engine.py:251-274 `aggregate_markout`, `self.done`, same method as dryrun) and updates on fills. 3b only ensures it is **surfaced during the canary**: confirm `print_summary`/status writes the per-horizon markout, and that live fills feed `self.done`. If already wired (likely), this is a verification + a test, not new code.
- RED: `test_live_fill_feeds_markout_gauge`, `test_status_includes_markout` (confirm the canary will actually record adverse-selection data — the whole reason to spend real money).

## F. Governance
- `decisions.md` **D12** — 3b mainnet canary enablement: dual-confirm (`allow_mainnet` + `LIVE_MAINNET_CONFIRM`), total-alloc cap (-1=full wallet), per-order -1 semantics + its danger, max-loss kill-switch. Note testnet dropped (dryrun-or-live).
- `config/strategy.yaml`: add `runtime.allow_mainnet: false`, `live.max_total_alloc_usd`, `live.max_loss_usd`; document `max_order_usd: -1` meaning.

---

# Implementation Plan (TDD, ordered; ≤3 files/task)

- **T1 — config resolvers + yaml + decisions** (`src/sca/config.py`, `config/strategy.yaml`, `docs/decisions.md`): `resolve_allow_mainnet`; yaml knobs; D12. [no deps]
- **T2 — caps in pure/client layer** (`src/sca/live/order_recon.py`, `src/sca/live/orders.py`): `-1` per-order semantics (clamp + assert skip); MakerOrderClient mainnet construction gated on an injected `allow_mainnet` (not just `testnet`). [deps T1]
- **T3 — engine gate + total-alloc cap + max-loss kill-switch** (`src/sca/live/engine.py`, `tests/test_engine_maker_runloop.py` or new `tests/test_phase3b.py`): `_compute_maker_enabled` mainnet branch; `_seed_slices_from_balance` + available-pool total-alloc cap; max-loss halt in `maker_step`; client construction passes mainnet flag. [deps T1,T2]
- **T4 — markout-on-live verification + tests** (tests only, maybe tiny engine touch): confirm live fills feed the gauge + status logs it. [deps T3]

Run command (all): `PYTHONPATH=src python3 -m pytest tests/ -q`. Baseline 294 must stay green; paper + testnet paths unchanged.

## Invariant → test map (new)
| Invariant | Test |
|---|---|
| mainnet needs BOTH allow_mainnet + LIVE_MAINNET_CONFIRM | `test_mainnet_refused_without_*`, `test_mainnet_allowed_with_dual_confirm` |
| testnet/paper default path unchanged | `test_testnet_path_unchanged_default` + existing 294 |
| total-alloc caps deployment; -1 = full wallet | `test_total_alloc_caps_deployment_below_wallet`, `test_total_alloc_minus1_uses_full_wallet`, `test_reconcile_respects_total_alloc_budget` |
| per-order -1 = no cap; finite still enforced | `test_clamp_minus1_no_cap`, `test_place_postonly_minus1_skips_assert`, `test_finite_cap_still_enforced` |
| max-loss halts + cancels all; disabled at 0; boundary | `test_max_loss_halts_and_cancels_all`, `test_max_loss_disabled_when_zero`, `test_loss_just_under_threshold_no_halt`, `test_halt_blocks_further_placement` |
| live fills measured (markout) | `test_live_fill_feeds_markout_gauge`, `test_status_includes_markout` |

## DoD (merge-ready ≠ run)
1. New RED→GREEN tests pass + existing 294 stay green; paper/testnet provably unchanged.
2. Mainnet provably unreachable without the dual confirm; cap enforced in the sizing path (not just stored); max-loss halt verified to cancel-all + stop.
3. ce/qa + one Codex heterogeneous review clean (no P0/P1).
4. NOT merged (owner's call), NOT run (owner provides keys + sets the canary env + executes).

## Operational canary run (owner, post-merge — documented, not executed by build)
Env for the FIRST small canary (example):
`MODE=live  LIVE_TRADING_CONFIRM=yes  LIVE_MAINNET_CONFIRM=yes  BYBIT_API_KEY=…  BYBIT_API_SECRET=…`
yaml: `runtime.testnet=false  runtime.allow_mainnet=true  runtime.maker_enabled=true  live.max_total_alloc_usd=300  live.max_order_usd=100  live.max_loss_usd=50`
→ `sca live`. Watch the dashboard + the markout gauge; scale caps up only after the markout/PnL data is acceptable. `-1` (unlimited) only after the canary validates sizing.

## Resolved design calls (for Codex)
- **Dual-confirm (not single flag) for mainnet:** real money must require a deliberate second env confirm; one config edit can't enable mainnet. Testnet stays the default — purely additive.
- **Total-alloc cap is the boss's "-1=use whole wallet" param** (per-order `max_order_usd` is separate, also gets -1). Canary needs BOTH small.
- **Max-loss reuses the existing halt plumbing** (no auto-reset; restart + human), checked each step before placement.
- **No new markout code** — 3a already measures it; 3b just guarantees it's logged on the live path.
