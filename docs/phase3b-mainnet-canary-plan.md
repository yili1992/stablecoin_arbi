# PHASE 3b — Mainnet canary, SIMPLIFIED (D14)

**Status:** implemented + merge-ready (NOT merged, NOT run). **Scope:** a tight DELTA on the
merged 3a maker layer — *mode rename + deletions + the single fund cap*. The owner's decision
(2026-06-19): the original 3b (D12/D13) had **too many parameters**; D14 cuts it to the minimum
usable surface. The real-money canary RUN remains the owner's separate action (keys + `MODE=live`).

> **Guiding principle (feedback_multi_mode_parity):** dryrun and live share IDENTICAL strategy
> logic + execution flow; the ONLY difference is the executor. So 3b/D14 is a **mode + config**
> change. It does **NOT** touch the maker order-lifecycle logic (reconcile / poll /
> cancel-to-terminal / persistence v2 / markout) hardened in 3a, nor the GTC bps-tiered
> buy/sell ladder.

---

## The model (D14 — two modes, one fund cap)

- **`runtime.mode` ∈ {`dryrun` (default), `live`}.** `resolve_mode` returns dryrun|live; any
  unknown value (including the legacy `paper`) coerces to **dryrun** — the safe default. A typo'd
  `MODE` can never select real money.
- **`dryrun`** = run the maker engine but **SIMULATE matching** (the original paper sim-fill off the
  live top-of-book). It builds **NO order client**, needs **NO API key**, places **NO real orders**.
  The markout (adverse-selection) gauge still records, exactly as before.
- **`live`** = real GTC PostOnly maker orders on **MAINNET (real money)**. `MODE=live` ALONE is the
  switch — **no extra confirm env**. A missing API key raises a clear `RuntimeError` at order-client
  construction (it never trades un-keyed; no silent downgrade).
- **Maker path switch == live mode:** `_compute_maker_enabled() == self.armed`, and
  `armed = live_authorization(mode) == (mode == "live")`.
- **The ONLY real-money fund limit: `live.max_total_alloc_usd`** (USD; `-1` = use the whole
  available wallet). Enforced in BOTH sizing entry points — `_seed_slices_from_balance` (valued at
  the coin's USD mark) and `_available_from_balance` (the reconcile re-quote pool). On a spot
  account the capital deployed IS the loss ceiling, so this single cap replaces the removed
  per-order cap AND the PnL max-loss kill-switch.

## Deleted vs D12/D13 (config + code + tests)

| Removed | Why |
|---|---|
| `runtime.testnet` / `maker_enabled` / `allow_mainnet` + their `resolve_*` | live is unconditionally mainnet; no venue gate, no rollback knob |
| `live.max_order_usd` (`_clamp_to_cap`, `desired_orders` cap arg, `place_postonly` assert) | per-order cap redundant under a total-alloc cap on spot |
| `live.max_loss_usd` whole PnL max-loss kill-switch (`_check_max_loss`, `_start_equity`, `_guard_mainnet_canary`, banner caps, the D13 `halted`/`start_equity` cross-restart persistence + `_guard_resumed_halt`) | capital cap = loss cap on a spot account |
| envs `LIVE_TRADING_CONFIRM` / `LIVE_MAINNET_CONFIRM` / `LIVE_UNCAPPED_CONFIRM` / `LIVE_CLEAR_HALT` | `MODE=live` alone = real money; no confirm chain |
| `MakerOrderClient` testnet/allow_mainnet gate (ctor/place mainnet refusal, sandbox flag) | live builds for mainnet directly |

## KEPT (3a order-lifecycle safety — NOT a PnL feature)

`_halt_operator_reconcile` (unattributable fill / cancel-never-terminal / reject-streak /
persist-failure halt — now an **in-memory** flag; a restart re-runs the R1 exchange-reconciliation
gate), `_cancel_to_terminal`, `_cancel_all_resting` (cancel-all-on-exit kill-switch + SIGINT/SIGTERM
handler), the fail-closed durable persist, and the entire maker order lifecycle
(reconcile_orders / poll_fills / _apply_exec_delta / match_live_orders / persistence v2 / markout) —
**unchanged**.

## Invariant → test map (D14)

| Invariant | Test (tests/test_phase3b.py) |
|---|---|
| default mode is dryrun (safe) | `test_default_mode_is_dryrun` |
| dryrun NEVER builds a client / NEVER places | `test_dryrun_default_never_builds_client_never_places` |
| MODE=live alone arms + builds a MAINNET client (no venue args) | `test_live_builds_client_and_can_place` |
| maker path switch == live mode | `test_compute_maker_enabled_tracks_live_mode` |
| live path can place a real order | `test_live_maker_path_places_real_order` |
| total-alloc caps deployment (seed + pool); -1 = full wallet; valued at USD mark | `test_total_alloc_caps_deployment_below_wallet`, `test_total_alloc_minus1_uses_full_wallet`, `test_total_alloc_caps_usd1_funded_side`, `test_reconcile_respects_total_alloc_budget`, `test_total_alloc_cap_offpeg_mark`, `test_total_alloc_cap_base_side_usd1` |
| live fills measured (markout) | `test_live_fill_feeds_markout_gauge`, `test_status_includes_markout` |
| cancel-all cancels EVERY resting order | `test_cancel_all_two_orders` |

Run: `PYTHONPATH=src python3 -m pytest tests/ -q` (all green; the count dropped from the 3a/3b
baseline because the deleted features' tests were removed).

## Operational canary run (owner, post-merge — documented, not executed by build)

```
MODE=live  BYBIT_API_KEY=…  BYBIT_API_SECRET=…           # MODE=live ALONE = real money
yaml: runtime.mode=live (or env MODE=live)  live.max_total_alloc_usd=300
```
→ `sca live`. Fund the **dedicated subaccount to exactly `max_total_alloc_usd`** (over-funding
refuses at the R1 exact-reconcile gate — conservative, correct). Real money should run **bare-metal
`sca live`** (no docker auto-restart), so a halt simply stops. Watch the dashboard + the markout
gauge; raise the cap only after the markout/PnL data is acceptable.

See `docs/decisions.md` **D14** (this model), D12/D13 (the superseded original 3b), D8/D10/D11.
