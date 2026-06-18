# PHASE 3a — Maker Order Primitive, Declarative Reconciliation & Real-Fill Slice Transitions (v5)

**Status:** design + TDD plan, REVISED after 3-persona self-review (architect / correctness / safety), a heterogeneous **Codex round-1** audit (block — 5×P0 / 10×P1 / 1×P2, all resolved in v3), **and Codex round-2** on v3 (block — 1×P0 / 2×P1, all one theme: *never clear local order state before the exchange confirms terminal truth*; resolved in v4). For implementation. **Scope:** 3a only. **Definition of done = merge-ready, NOT merged, NOT live.** All real placement is testnet-only and stays behind the existing triple gate + R1 reconcile gate. 3a does **not** validate strategy economics (that is 3b).

> **Review-fix traceability (three layers).** v1-review fixes keep `(✔F<n>/P<tier>)`. Codex round-1 fixes are `(✔C-P0#n)`/`(✔C-P1#n)`/`(✔C-P2#n)`. Codex **round-2** fixes are `(✔R2-P0)`/`(✔R2-P1)`: **R2-P0** = cancel must POLL to terminal (`_cancel_to_terminal`) and never clear while `status_class=='open'`/PendingCancel; **R2-P1** = ambiguous/unknown orders go to a separate **unattributed** list (never a guessed `slice_idx`) and any executed qty on one ⇒ `_halt_operator_reconcile`. Codex **round-3** fixes are `(✔R3-P0/P1/P2)`: **R3-P0** = `maker_step` runs `poll_fills` BEFORE `reconcile_orders` (+ reconcile terminal-syncs any persisted-but-vanished order before placing) so a completed order is never overwritten unbooked; **R3-P1** = `_cancel_to_terminal` takes the injected `client`, and `_cancel_all_resting` (kill-switch) routes through it (poll+book before clear, fail-closed); **R3-P2** = F6 parity prose corrected (`dq=nq=R·q/B`). Structure preserved (D0 / Part A A1–A10 / Part B TDD / file list / invariant→test map / rollback / DoD / resolved decisions); a revision, not a rewrite.

> **Codex grounding (ccxt 4.5.54 `bybit.py`, read directly — authoritative).** Order-state retrieval was the crux of the block: the singular `fetch_open_order(id)`/`fetch_closed_order(id)` key on **orderId only** (bybit.py:5049, 5019) and `fetch_closed_order`→`fetch_closed_orders` is hard-coded **`orderStatus='Filled'`** (bybit.py:5215) → it **cannot see Cancelled/Rejected**. The correct mechanism is `fetch_open_orders(symbol, params={'orderLinkId':link})` for the open/partial state and `fetch_canceled_and_closed_orders(symbol, params={'orderLinkId':link})` (bybit.py:5081 → `/v5/order/history`) for the terminal Filled-OR-Cancelled/Rejected state. ccxt `parse_order` exposes our link at `order['clientOrderId']` (=`orderLinkId`, bybit.py:3711, 3750-3752), `id`=`orderId` (3725), `filled`=`cumExecQty` (3735), `remaining`=`leavesQty` (3736), `avgPrice` (3753), `status`←`orderStatus` (3739). `edit_order_request` (bybit.py:4252-4309) accepts **no postOnly/timeInForce** → amend is qty-only. Duplicate clientOrderId → retCode **170141** (InvalidOrder, :896) / **12141** (BadRequest, :679) / **30001** (BadRequest "order_link_id is repeated", :1030).

---

## D0 — Execution model: MAKER (DECIDED — not re-asked)  `(✔F1/P0)`

The owner has **explicitly re-locked execution to MAKER** (PostOnly resting ladder). This supersedes the older TAKER (IOC marketable-limit) lock recorded in `docs/live-bybit-readonly-r1-plan.md`. **This is decided; 3a does not re-open it.**

**Why MAKER is well-defined here:** rung prices are deterministic (`R_i = anchor + rung_bp_i/1e4`, rebuy `B = anchor + rebuy_off_bp/1e4`); the anchor updates only on **closed 1h candles**, so pre-placing resting PostOnly limits at known levels is fully specified and *captures* (does not pay) the half-spread — strictly better than crossing if fill probability is adequate (3b measures that).

**Governance deliverable (Task 7, `(✔F1/P0)`):** the re-lock must be recorded so the docs stop contradicting each other:
1. New `docs/decisions.md` entry **D11 — execution model re-locked TAKER → MAKER (supersedes the R1 TAKER lock)**.
2. A note in `docs/live-bybit-readonly-r1-plan.md` that the Phase-3 execution model is now **MAKER** (the line "trading via **taker** … applies in Phase 3" is superseded).
3. The maker-aware `reconcile.py` change (Task 3) is the *code* half of this governance change: a maker strategy leaves resting orders **by design**, so the R1 "any open order ⇒ anomaly" rule must become maker-aware.

---

# PART A — DESIGN

## A1. Goal

Build the **safe maker primitive + plumbing + declarative reconciliation** so the armed-live engine, on **testnet**, places/cancels/amends resting **PostOnly GTC** limit orders at the deterministic rung/rebuy prices, drives slice-state transitions from **real (incl. partial) fills**, and persists an `orderId↔slice` map that survives crash/restart. Paper mode is untouched and remains the safe default. No mainnet placement in 3a; no economics claims.

## A2. Component boundaries

### New module 1 — `src/sca/live/order_recon.py` (PURE, no ccxt, no I/O)

The deterministic core. Zero network, unit-testable with hand-built dicts (mirrors `reconcile.py`).

```
# 1bp-tick precision (SINGLE source of truth — orders.py imports these, must NOT re-derive)
TICK = 0.0001          # passed in from market meta; default mirrors engine TICK_DP=4
def floor_to_tick(x, tick) -> float        # round DOWN to grid
def ceil_to_tick(x, tick) -> float         # round UP to grid
def quantize_price(side, raw, tick) -> float
    # BUY  -> floor_to_tick (never cross up into asks)
    # SELL -> ceil_to_tick  (never cross down into bids)
def quantize_qty(qty, lot) -> float        # floor to lot step

@dataclass(frozen=True)
class Desired:  side: str; price: float; qty: float      # side in {"buy","sell"}
@dataclass(frozen=True)
class Live:     order_id: str|None; link_id: str|None; side: str; price: float; qty: float
                filled_qty: float = 0.0          # (✔C-P1#9) cumExecQty on the resting order
                matched_by: str|None = None      # (✔C-P1#10) "link_id"|"order_id"|"approx"
                # price/qty are the REMAINING (leaves) resting size — see A3 amend semantics (F8)
@dataclass(frozen=True)
class Action:   kind: str; slice_idx: int; desired: Desired|None; live: Live|None
                # kind in {"place","cancel","amend","leave"}   # NO "refuse": an ambiguous order is
                # NOT an action on a guessed slice; it goes to the UNATTRIBUTED list (✔R2-P1)

def desired_orders(anchor, slices, rungs, rebuy_off_bp, tick, lot,        # +lot (✔F17/P2)
                   avail_base, avail_quote, min_qty, min_cost,            # +min_* (✔F19/P2)
                   max_order_usd) -> dict[int, Desired]                   # +cap   (✔F11/P1)
def match_live_orders(persisted_slices, open_orders)                      # (✔C-P1#10, ✔R2-P1) PURE
        -> tuple[dict[int, Live], list[Live]]:    # returns (MATCHED_by_slice, UNATTRIBUTED)
    # precedence per open order: exact order_link_id (clientOrderId) -> exact order_id
    #   -> UNAMBIGUOUS approx (side, price≈) with EXACTLY ONE candidate slice.
    # An order mapping to NO slice, to >1 candidate (same-price-rebuy ambiguity), or a stale
    #   sca-* whose (idx,gen) no longer matches -> appended to the UNATTRIBUTED list with NO
    #   slice identity (never forced onto a guessed slice_idx). Engine cancel-to-terminal + halts
    #   on it (A3); it is never used to clear a slice's order state.
def diff_orders(desired, matched, price_tol, qty_tol) -> list[Action]
    # `matched` = the slice-attributed Live map ONLY; price_tol = 1 tick (✔F15); qty_tol = lot/2 (✔F17)
    # kinds: place|cancel|amend|leave. Ambiguity handled out-of-band via the unattributed list.
```

**Dependencies:** none (stdlib only). **Why pure:** lets us test "anchor moves <1 tick ⇒ identical desired ⇒ all `leave`", aggregate-avail bounding, **link_id→id→approx precedence**, and same-price ambiguity refusal **without any exchange** (✔C-P1#10 — matching now has a single, pure, tested home; it is no longer buried in the engine/diff).

### New module 2 — `src/sca/live/orders.py` (the maker order client)

The only file allowed to cross the no-order boundary. Mirrors `BybitPrivateClient` construction exactly (`mod.bybit({... 'enableRateLimit':True, 'options':{'defaultType':'spot'} ...})`, `set_sandbox_mode(True)` for testnet). It adds **no** order method to `BybitPrivateClient`; the tested invariant `test_client_exposes_no_order_methods` (`tests/test_bybit_client.py:133`) stays intact (✔C-P1#6).

```
class MakerOrderClient:
    def __init__(self, *, ccxt_module=None, live_cfg=None, env=None, testnet=None)
        # reuse sca.live.creds.resolve(); sandbox if testnet; assert spot.
        # HARD: refuse to even CONSTRUCT on mainnet in 3a (testnet is False -> raise) (✔F21/P2)
        self.max_order_usd = float(live_cfg.get("max_order_usd", ...))   # read the cap (✔F11/P1)
    def market_meta(self, symbol) -> dict          # {tick, lot, min_qty, min_cost}
    def place_postonly(self, symbol, side, price, qty, link_id) -> dict
    def amend(self, symbol, order_id, *, link_id=None, qty=None) -> dict  # qty-ONLY, NO TIF/postOnly (✔C-P1#7)
    def cancel(self, symbol, order_id, *, link_id=None) -> dict
    def fetch_order_state(self, symbol, order_id=None, *, link_id=None) -> dict   # open-then-terminal (✔C-P0#1/#2)
    def fetch_open(self, symbol) -> list[dict]     # ALL open orders for symbol; each MUST expose clientOrderId (✔F5/P1)
```

Grounded specifics baked in:

- **place_postonly** → `create_order(symbol,'limit',side,qty,price,{'postOnly':True,'isLeverage':0,'clientOrderId':link_id})`. ccxt sets `timeInForce='PostOnly'` (never pass GTC alongside). `isLeverage:0` explicit (no margin borrow). `price`/`qty` are **pre-snapped strings from `order_recon.quantize_*`** — we do NOT trust ccxt's `price_to_precision` (it ROUNDs and can cross; only `amount_to_precision` floors). `create_order` returns `{id, link_id}` with `status/filled=None` → **accepted, not filled** → caller MUST re-poll. **HARD ASSERT (✔F11/P1):** `price*qty <= self.max_order_usd` is asserted at the top of `place_postonly` and **raises** (not logs) on violation — the last line of defence even if `desired_orders` mis-sizes.
- **PostOnly reject** is NOT a clean exception. Handle BOTH grounded paths: (a) returned order `status=='canceled'`/`'rejected'` + `info['rejectReason']=='EC_PostOnlyWillTakeLiquidity'`; (b) raised `ccxt.InvalidOrder`/`ExchangeError`. Both → classified `'postonly_rejected'` → "price moved, skip & re-quote next tick" (NOT an error). Engine then applies a **per-slice cooldown** — see A8a (✔F9/P1).
- **min-size InvalidOrder (✔F19/P2):** an `InvalidOrder` whose cause is below-minimum qty/cost is classified **`'too_small'`** — logged-and-skipped, distinct from `postonly_rejected`, and **never hot-retried**. (Defence-in-depth behind `desired_orders` already dropping sub-min orders.)
- **Idempotency — ALL dup-clientOrderId codes (✔C-P2#16, supersedes v2's 170141-only):** a duplicate `link_id` maps to **retCode 170141** (`ccxt.InvalidOrder`, bybit.py:896) **OR 12141** (`ccxt.BadRequest`, :679) **OR 30001** (`ccxt.BadRequest` "order_link_id is repeated", :1030). Treat **ANY** of these codes/messages (match on retCode set {170141, 12141, 30001} **and** on a "duplicate"/"repeated" message substring as a belt-and-braces fallback) as the idempotent **"already placed → `fetch_order_state(link_id=…)` to learn truth"** path. A network-uncertain retry with the **same** link_id can therefore never double-fill regardless of which code Bybit returns.
- **fetch_order_state — open-then-terminal by link (✔C-P0#1, ✔C-P0#2; SUPERSEDES v2's `fetch_open_order`/`fetch_closed_order`):** v2 keyed on `orderId` and fell back to `fetch_closed_order`, which is hard-coded `orderStatus='Filled'` (bybit.py:5215) and therefore **cannot observe a Cancelled/Rejected order or a cancelled-partial** — a PostOnly-reject or cancel-with-residual was invisible. v3 mechanism:
  1. **OPEN state** (still resting, incl. partial fill): `ex.fetch_open_orders(symbol, params={'orderLinkId': link})` → list of 0 or 1. Non-empty ⇒ the order is still open; normalize that row. (By orderId: pass `{'orderId': id}` in params, or the singular `fetch_open_order(id, symbol)`.)
  2. **Absent from open ⇒ TERMINAL state** (Filled **OR** Cancelled/Rejected): `ex.fetch_canceled_and_closed_orders(symbol, params={'orderLinkId': link})` (bybit.py:5081 → `/v5/order/history`). Pick the row for our link/id; normalize.
  - **Never** call `fetch_order`, `fetch_closed_order`, or `fetch_closed_orders` (the Filled-only path that hides cancels/rejects).
  - Normalizes from raw V5 fields → `{id, link_id, status, status_class, filled (=cumExecQty), remaining (=leavesQty), avg (=avgPrice), price, reject_reason (=rejectReason), raw}`. `status_class` distinguishes `open` | `filled` | `postonly_rejected` | `cancelled` | `rejected`. **`open` is NON-terminal and explicitly includes Bybit `New`/`PartiallyFilled`/`PendingCancel`** (✔R2-P0) — `PendingCancel` is the transient post-cancel state whose leaves can STILL fill, so `_cancel_to_terminal` treats `open` as "keep polling" and never clears local state until a TERMINAL class (`filled`/`cancelled`/`rejected`/`postonly_rejected`) is observed. **Asserts `filled` isFinite** before returning; downstream additionally guards `None`/non-finite (✔F20/P2).
  - **May be called by link_id alone** (id=None) for crash-resume of an order whose id was never persisted (✔F14/P1) — both endpoints accept `orderLinkId` in params.
- **fetch_open(symbol)** returns the **full** normalized open-order list for the symbol (`[normalize(o) for o in ex.fetch_open_orders(symbol)]`), each exposing `clientOrderId` (=our link_id) so `match_live_orders` can map exchange truth → slices (✔F5/P1, ✔C-P1#10). This is distinct from the single-order `fetch_order_state` (link-filtered) above.
- **amend semantics — qty-ONLY, no TIF/postOnly (✔C-P1#7, corrects v2):** `/v5/order/amend`'s ccxt request schema (`edit_order_request`, bybit.py:4252-4309) accepts **only** symbol / orderId-or-orderLinkId / category / qty / price (+ trigger/SL/TP) — **there is no postOnly or timeInForce field**. v2's "re-assert postOnly/timeInForce on the amend payload" was unsupported and is **removed**. The exchange **preserves** the original order's PostOnly/TIF across an amend, so nothing needs re-asserting. Therefore:
  - `amend` sends **qty only** (`edit_order(id, symbol, 'limit', side, amount=qty)`, or via `params={'orderLinkId': link}`), then **re-polls `fetch_order_state` to verify** the new leaves qty.
  - It **refuses any price change** (raises) — a re-price is a `cancel + recreate`, never an amend.
  - **NEVER amend a partially-filled order** (`cumExecQty>0` / our `filled_qty>0`): route to **cancel + recreate** instead, *after* booking the residual fill (A3 cancel path, ✔F7/P1, ✔C-P0#3). Bybit amend sets **TOTAL** qty (new resting leaves = `newQty − cumExecQty`); amending a partial mid-flight races the fill and corrupts leaves accounting.
  - The pure diff compares **remaining-to-remaining**: `Live.qty` is built from `leavesQty` (the resting remainder), not the original order qty (✔F8/P1).
  - thin return `{id,clientOrderId}` → must re-poll.
- **429 backoff:** `except ccxt.RateLimitExceeded`(subclass of NetworkError)/`DDoSProtection` → exponential `1→2→4→…cap 30s`, honor `Retry-After`/`rate_limit_reset_ms`, bounded retries, then re-raise. Clean except split: `RateLimitExceeded`(backoff) / `InvalidOrder`+`BadRequest`(postonly_rejected | too_small | idempotent-dup{170141,12141,30001}) / `InsufficientFunds`(skip+log).

**Dependencies:** `ccxt` (injectable `ccxt_module=`), `sca.live.creds`, `sca.live.order_recon` (precision helpers + `match_live_orders`).

### Modified — `src/sca/live/bybit_client.py` (read-only shape only)  `(✔C-P1#6)`

`normalize_order` (bybit_client.py:78-86) currently drops `clientOrderId`. Add **one read-only field** so the R1 gate's account-wide open-order list carries our link, letting `resume_reconcile_orders` (which reuses that exact list — ✔F23) match by link_id:

```
def normalize_order(o: dict) -> dict:
    return {"id": o.get("id"), "symbol": o.get("symbol"), "side": o.get("side"),
            "price": o.get("price"), "qty": o.get("amount"), "type": o.get("type"),
            "clientOrderId": o.get("clientOrderId")}      # (✔C-P1#6) keep link; still READ-ONLY
```

This adds **NO order method** — `BybitPrivateClient` stays order-less, so `test_client_exposes_no_order_methods` (`test_bybit_client.py:133`) stays green untouched. The only deliberate test change is the open-order **shape** assertion `test_get_open_orders_normalizes` (`:174`), which now also asserts `clientOrderId` is present. (This is why the file list moves `bybit_client.py` from *Untouched* to *Modified* — a single read-only field, no logic.)

### Modified — `src/sca/live/engine.py`

Adds an injectable order-client seam and the maker fill loop; leaves paper untouched.

- ctor: `self.order_client = None` + injectable; lazily built `MakerOrderClient(testnet=resolve_testnet())` only on the armed-maker path. Also init `self._r1_ok = False` (✔F22/P2), `self._r1_report = None`, `self._r1_open_orders = None` (✔C-P0#5 — gate stores its decision + fetched open-orders for resume to reuse), and per-slice reject cooldown state (✔F9/P1).
- **`self.maker_enabled = self.armed and resolve_testnet() and resolve_maker_enabled()`** — three **independent logical flags**, NOT a sentinel. The added `resolve_maker_enabled()` (✔C-P1#14) is the explicit rollback knob: `env > runtime.maker_enabled > default(false)`. Flip it off → engine reverts to the paper `evaluate_fills` path with zero behavior change.
- New methods: `reconcile_orders(now, client=None)`, `poll_fills(now, client=None)`, `maker_step(now)`, `resume_reconcile_orders(open_orders, client=None)` (takes the **gate-fetched** list, ✔F23/P2, ✔C-P0#5), `_persist_durable_or_halt()`, `_cancel_all_resting(client=None)` (✔F12/P1), `_seed_slices_from_balance(bal)` (✔F3/P1, now called INSIDE the gate — ✔C-P0#5), `_apply_exec_delta(i, st, now)` (shared book-a-fill helper used by poll, cancel-rebook, and resume), `_available_from_balance(bal, live)` (free + own-locked sizing, ✔C-P1#13), `_cancel_to_terminal(order_id, link_id, now, slice_idx=None, client=None)` (cancel + poll-until-terminal + book; client threaded ✔R2-P0/✔R3-P1), `_halt_operator_reconcile(reason)` (cancel-all + refuse for unattributable fills; ✔R2-P1). `_cancel_all_resting` itself routes every persisted resting order through `_cancel_to_terminal` (poll terminal + book final fill before clearing; ✔R3-P1).
- `evaluate_fills` and `_maybe_deploy` are **bypassed when `maker_enabled`** (real fills replace them) — see A4b for exact guard sites; paper still calls them verbatim → existing 130 tests stay green.
- **State-switched readers generalized for partial fills (✔F2/P0, all 3 personas) + valuation split by leg (✔C-P1#12):** `_usd1_qty`, `_slice_value`, `_local_summary`, **and `status_doc`'s aggregation** must value/sum legs **independent of `state`** — see A4a.

### Modified — `src/sca/live/reconcile.py`

Invert the "any resting order ⇒ anomaly" rule: a maker strategy leaves orders by design (✔F1/P0). Add an `expected` parameter (set of our `link_id`s / expected `(side,price,qty)`); empty ⇒ old taker behavior (refuse on orders) preserved for the 13 existing tests. **Ownership (✔F23/P2):** `reconcile.py` *decides* `proceed | refuse | fresh_deploy` from balances + expected orders; it performs **no I/O and no side-effects**. The engine's `resume_reconcile_orders` performs the side-effecting cancel/apply using the **same already-fetched** open-orders list the gate stored (no refetch — ✔C-P0#5).

### Modified — `src/sca/live/persistence.py` + engine resume

Schema bump `v=1 → v=2` with **v1→v2 migration** (inject default order fields **incl. `sell_proceeds=0.0`, `qty_sold=0.0`** — ✔C-P1#8; safe because any v1 state is pre-maker paper with no live orders and no in-flight sell cycle). Sync ALL paths: `_state_dict`, `_maybe_resume` type-check + restore. On the armed-maker path, fill logging routes through a **fail-CLOSED** primitive (`_persist_durable_or_halt`) and `load_state` corrupt/missing is **gated behind exchange reconciliation** (A9 / D10), never a silent fresh deploy.

### Modified — `config/strategy.yaml` + `src/sca/config.py`

`runtime.testnet: true` (3a default) and `runtime.maker_enabled` knob. **Params in YAML, not code** (hard rule #1). Two resolvers mirroring `resolve_mode` precedence (`env > runtime > default`):
- **`config.resolve_testnet(cfg,env)` is the SINGLE testnet resolver (✔F13/P1)**; `live.testnet` is **deprecated/redirected** to `runtime.testnet` (one reads the other) so the R1 gate and the maker client can never be on different venues (no split-brain).
- **`config.resolve_maker_enabled(cfg,env)` (✔C-P1#14)** — the rollback knob, `env > runtime.maker_enabled > default(false)`.

## A3. Declarative order-reconciliation algorithm

### Slice dict (additions to the 5 existing fields)

```
"order_id":      str|None    # exchange orderId (None = no live order, or acked-but-id-not-yet-persisted)
"order_link_id": str|None    # our clientOrderId, persisted for idempotent retry/restart match
"order_px":      float|None  # quantized price we placed at
"order_side":    str|None    # "buy"|"sell"
"order_qty":     float|None  # TOTAL qty we placed (base units)
"filled_qty":    float       # cumulative exec on the CURRENT order (default 0.0)
"order_gen":     int         # generation counter; bumped on re-price -> new link_id
"reject_streak": int         # consecutive PostOnly rejects for this slice (cooldown/halt, ✔F9/P1)
"sell_proceeds": float       # (✔C-P1#8) cumulative QUOTE received from sells this cycle (default 0.0)
"qty_sold":      float       # (✔C-P1#8) cumulative BASE sold this cycle, for blended avg_sell (default 0.0)
```

`order_link_id` scheme (≤36 chars, deterministic): `f"sca-{slice_idx}-{order_gen}"`. **Idempotency vs re-pricing tension resolved:** a pure retry reuses the persisted `order_link_id` (Bybit dedupes via 170141/12141/30001 → no double-fill); a genuine re-price bumps `order_gen` → a fresh link_id.

### Desired set (pure) — with aggregate-avail bound, min-size drop, and notional cap

```
def desired_orders(anchor, slices, rungs, rebuy_off_bp, tick, lot,
                   avail_base, avail_quote, min_qty, min_cost, max_order_usd):
    out = {}
    pool_base, pool_quote = avail_base, avail_quote          # running pools (✔F16/P2)
    for i, s in enumerate(slices):
        if s["state"] == "usd1":                              # want resting SELL at rung
            raw = anchor + rungs[i] / 1e4
            px  = quantize_price("sell", raw, tick)           # CEIL -> never cross down
            qty = quantize_qty(min(s["qty"], pool_base), lot)
        else:                                                 # "usdt" -> want resting BUY at rebuy
            raw = anchor + rebuy_off_bp / 1e4
            px  = quantize_price("buy", raw, tick)            # FLOOR -> never cross up
            qty = quantize_qty(min(s["cash"]/px, pool_quote/px), lot)
        # notional cap (✔F11/P1): clamp qty so qty*px <= max_order_usd; if even one lot exceeds -> drop
        qty = _clamp_to_cap(qty, px, lot, max_order_usd)
        # min-size drop (✔F19/P2): below min_qty OR notional below min_cost -> emit NOTHING for this slice
        if qty < min_qty or qty * px < min_cost:
            continue
        out[i] = Desired(s_side(s), px, qty)
        if s["state"] == "usd1": pool_base  -= qty            # decrement pools so aggregate
        else:                    pool_quote -= qty * px        #   committed base/quote is bounded (✔F16/P2)
    return out
```

**Determinism / hysteresis "within one tick bucket ⇒ zero touch" (✔F15/P2):** `quantize_price` snaps `anchor+offset` to the grid; `diff_orders` re-prices **only when** `|new_px − resting_px| ≥ 1 full tick`. If the anchor shifts by less than one tick, every `px` is identical to the prior tick ⇒ all `leave` ⇒ **zero orders touched** ⇒ queue priority preserved. A **≥1bp anchor move** (one tick) re-prices the affected rungs — acceptable at the 1h anchor cadence.

**Aggregate availability bound (✔F16/P2):** the running `pool_base/pool_quote` are decremented as each slice's desired order is allocated, so the aggregate committed base/quote is bounded **before** any order hits the exchange. Under-arming is then **explicit** (a later slice deterministically gets a smaller/zero order), not `InsufficientFunds`-driven.

### Available-balance pools — locked-vs-free, precise  `(✔C-P1#13)`

`avail_base/avail_quote` fed to `desired_orders` must be neither raw `free` nor raw `wallet`:

```
def _available_from_balance(self, bal, live):    # live = match_live_orders(...) map
    free_base,  free_quote  = free(bal, base_coin),  free(bal, quote_coin)   # wallet - locked
    own_locked_base  = sum(l.qty for l in live.values() if l.side == "sell") # leaves of OUR resting SELLs
    own_locked_quote = sum(l.qty * l.price for l in live.values() if l.side == "buy")
    avail_base  = free_base  + own_locked_base       # our own resting base is re-deployable (cancel+re-price)
    avail_quote = free_quote + own_locked_quote       # our own resting quote likewise
    return avail_base, avail_quote
```

- **NOT raw `free`:** raw free excludes funds already locked in *our own* valid resting orders, so sizing against it would needlessly shrink/force-cancel orders we intend to keep (kills queue position, the whole point of maker).
- **NOT raw `wallet`:** wallet includes funds locked in *foreign/other* holds, so sizing against it would overcommit → `InsufficientFunds`.
- **Validation of any NEW or INCREASED placement is against POST-cancel free (✔C-P1#13):** when the apply loop sends a `place` (or an amend-up), it admits it only if its incremental notional ≤ `free + (funds this tick's cancels for that coin will release)`. A `leave` consumes nothing new. This guarantees no `InsufficientFunds` while preserving queue priority. RED: `test_avail_uses_free_plus_own_locked`.

### Queue-preserving diff (pure)

```
def diff_orders(desired, matched, price_tol, qty_tol):   # `matched` = slice-attributed Live map ONLY
    actions = []                                          # price_tol = 1 tick (✔F15); qty_tol = lot/2 (✔F17)
    for i in all_slice_indices:                           # ambiguity is NOT here — it's the unattributed
        d, l = desired.get(i), matched.get(i)             #   list handled out-of-band in reconcile_orders (✔R2-P1)
        if d is None and l is None:        continue
        if d is None and l is not None:    actions.append(Action("cancel", i, None, l)); continue
        if d is not None and l is None:    actions.append(Action("place",  i, d, None)); continue
        same_side  = (l.side == d.side)
        same_price = abs(l.price - d.price) < price_tol      # < 1 tick => same bucket => leave
        if same_side and same_price:
            if abs(l.qty - d.qty) <= qty_tol:                # l.qty is REMAINING/leaves (✔F8)
                actions.append(Action("leave", i, d, l))     # PRESERVE QUEUE
            elif d.qty < l.qty and l.filled_qty == 0:        # qty-DOWN on an UNFILLED order -> amend (✔C-P1#9 uses Live.filled_qty)
                actions.append(Action("amend", i, d, l))     #   keeps queue position
            else:                                            # qty-up, OR any partially-filled order (✔F8)
                actions.append(Action("cancel", i, None, l)) #   cancel (booking residual, A3-apply F7/C-P0#3)
                actions.append(Action("place",  i, d, None)) #   + recreate
        else:                                                # side or price changed
            actions.append(Action("cancel", i, None, l))
            actions.append(Action("place",  i, d, None))
    return actions
```

### Order↔slice matching — PURE `match_live_orders` (`link_id` AUTHORITATIVE) `(✔F5/P1, ✔C-P1#10)`

Matching lives in **one pure function**, `order_recon.match_live_orders(persisted_slices, open_orders)` (no engine, no ccxt), so precedence and ambiguity are unit-testable with hand-built dicts. Exchange truth (the `open_orders` list, each carrying `clientOrderId`) wins over local memory. **Match precedence per slice:**
1. **EXACT `order_link_id`** (`sca-{idx}-{gen}` == order's `clientOrderId`) — authoritative on the maker path → `Live(matched_by="link_id")`.
2. then exact `order_id` → `Live(matched_by="order_id")`.
3. only if BOTH are unavailable, an approximate `(side, price≈, qty≈)` fallback → `Live(matched_by="approx")`.

Each `Live` carries `filled_qty = cumExecQty` (✔C-P1#9) and `qty = leavesQty`.

**Same-price-rebuy ambiguity is fatal, never guessed (✔F5/P1, ✔C-P1#10, ✔R2-P1):** if `link_id` is unavailable AND the approximate fallback has **>1 candidate slice** at the same `(side, price≈)` (e.g. two slices both resting BUYs at `anchor−1bp`), the order is **NOT** mapped to any slice — it is appended to the returned **unattributed** list (with no `slice_idx`). The engine then `_cancel_to_terminal`s it and, if any qty executed, `_halt_operator_reconcile`s (it cannot know which slice the fill belongs to); a clean cancel is logged. Round-1 instead emitted `Action("refuse")` on a *guessed* `slice_idx` and cleared that slice's order state — which could clear the wrong slice and (worse) drop a fill on an ambiguous partially-filled order; both are fixed by routing ambiguity through the unattributed list + cancel-to-terminal (✔R2-P1). This relies on `fetch_open`/`get_open_orders` exposing `clientOrderId` (✔C-P1#6, asserted in Task 1/2); the ambiguity guard is the safety net if Bybit ever returns it absent. RED: `test_match_live_orders_returns_ambiguous_in_unattributed`, `test_unattributed_order_with_fill_halts_operator_reconcile`, `test_live_has_filled_qty`.

### Apply (engine, ordered to avoid double fund-lock / dup link_id; cancel-first rebooking; stale-place abort)

```
def reconcile_orders(self, now, client=None):
    assert self._r1_ok, "reconcile_orders before R1 gate"   # (✔F22/P2)
    if not self.maker_enabled: return
    if self.anchor is None:    return                        # (✔F19/P2) no anchor -> no desired set
    client = client or self.order_client
    meta = client.market_meta(self.symbol)
    matched, unattributed = match_live_orders(self.slices, client.fetch_open(self.symbol))  # (✔C-P1#10, ✔R2-P1)
    # (✔R2-P1) Ambiguous / unknown live orders are NEVER mapped to a guessed slice. Resolve them
    # out-of-band: cancel-to-TERMINAL, then if ANY qty executed on an unattributable order we
    # cannot safely book it -> HALT for operator reconciliation. Never clear a slice for these.
    for u in unattributed:
        st = self._cancel_to_terminal(u.order_id, u.link_id, now, client=client)  # slice_idx=None -> books nothing (✔R3-P1)
        if st["filled"] and st["filled"] > 0:
            self._halt_operator_reconcile(f"fill on unattributable order {u.link_id or u.order_id}")
        # else: a clean stray-order cancel -> log only; touch NO slice state
    avail_base, avail_quote = self._available_from_balance(client.balance(), matched)  # free+own-locked (✔C-P1#13)
    desired = desired_orders(self.anchor, self.slices, self.rungs, REBUY_OFF_BP,
                             meta["tick"], meta["lot"], avail_base, avail_quote,
                             meta["min_qty"], meta["min_cost"], client.max_order_usd)
    aborted = set()                                          # (✔C-P0#4) slices whose precomputed place is now stale
    for a in diff_orders(desired, matched, meta["tick"], meta["lot"]/2):
        if a.kind == "leave":   continue
        if a.kind == "place" and a.slice_idx in aborted:     # (✔C-P0#4) a prior cancel changed this slice's state
            continue                                          #   -> DROP the stale precomputed place; re-derive next tick
        if self._in_cooldown(a.slice_idx, desired.get(a.slice_idx)):  # (✔F9/P1) skip rejected rung
            continue
        if a.kind == "cancel":
            # (✔C-P0#3, ✔R2-P0) cancel FIRST, then POLL to TERMINAL (never clear while status_class
            # is "open"/PendingCancel — leaves could still fill), book the FINAL exec, THEN clear.
            # _cancel_to_terminal does the bounded poll + booking; returns terminal st incl "changed".
            st = self._cancel_to_terminal(a.live.order_id, a.live.link_id, now, slice_idx=a.slice_idx, client=client)  # (✔R3-P1)
            if st.get("changed"):                             # (✔C-P0#4) booked a delta or flipped state ->
                aborted.add(a.slice_idx)                      #   abort the paired place; recompute next tick
        elif a.kind == "amend":                              # qty-only, unfilled order (diff guaranteed), NO TIF (✔C-P1#7)
            client.amend(self.symbol, a.live.order_id, link_id=a.live.link_id, qty=a.desired.qty)
            self.slices[a.slice_idx]["order_qty"] = a.desired.qty; self._persist_durable_or_halt()
        elif a.kind == "place":
            s = self.slices[a.slice_idx]; s["order_gen"] += 1
            link = f"sca-{a.slice_idx}-{s['order_gen']}"
            # persist INTENT (link_id+gen) BEFORE the network call -> crash never orphans
            s["order_link_id"] = link; s["order_side"] = a.desired.side
            s["order_px"] = a.desired.price; s["order_qty"] = a.desired.qty
            self._persist_durable_or_halt()
            r = client.place_postonly(self.symbol, a.desired.side,
                                      a.desired.price, a.desired.qty, link)
            if r.get("status_class") == "postonly_rejected":
                self._note_reject(a.slice_idx); self._clear_slice_order(a.slice_idx)  # cooldown (F9)
            elif r.get("status_class") == "too_small":
                self._clear_slice_order(a.slice_idx)         # (✔F19/P2) logged-skip, not retried
            else:
                self._reset_reject(a.slice_idx); s["order_id"] = r["id"]
            self._persist_durable_or_halt()
```

**Cancel-to-terminal rebooking (✔C-P0#3 + ✔R2-P0 — supersedes v2's "fetch before cancel"):** v2 fetched state *before* cancelling, dropping any fill in the fetch→cancel window. v3 cancels **first** via `_cancel_to_terminal`, which then **polls `fetch_order_state` until a TERMINAL status** (filled/cancelled/rejected). The round-1 fix did a *single* post-cancel poll, but Bybit can return `PendingCancel`/still-open immediately after a cancel and the resting leaves can **still fill** in that window (✔R2-P0); a single poll could read "open", and clearing on it would lose a later fill. So `_cancel_to_terminal` keeps polling (bounded backoff) and **never clears/persists-cleared while `status_class=='open'`**; it books the **final** `cumExecQty` delta from the terminal row and only then clears. If terminal is never reached within the bounded retries it **halts fail-closed** rather than clear on an unknown outcome. The terminal fetch is authoritative for "what actually executed," so no fill is ever lost to a cancel race.

**Unattributable / ambiguous orders → cancel-to-terminal + operator halt (✔R2-P1):** `match_live_orders` returns the slice-attributed map plus an **unattributed** list (an open order matching no slice, matching >1 same-price slice, or a stale `sca-*` whose (idx,gen) is gone). The engine resolves each unattributed order with the **same `_cancel_to_terminal`** path but with **no slice_idx** (so it books to no slice and clears no slice state). If the terminal state shows **any executed qty** on an order we cannot attribute, the engine **cannot safely book it** → `_halt_operator_reconcile` (cancel-all + refuse, surface to the operator) rather than guess a slice. A clean stray cancel (zero fill) is just logged. This removes round-1's `Action("refuse")`, which incorrectly cancelled-and-cleared a *guessed* slice (✔R2-P1). RED: `test_unattributed_order_with_fill_halts_operator_reconcile`, `test_match_live_orders_returns_ambiguous_in_unattributed`, `test_cancel_polls_through_pending_cancel_until_terminal`.

**Stale-place abort across a state change (✔C-P0#4):** `diff_orders` precomputes `(cancel, place)` pairs from the desired/live snapshot at the **top** of the tick. If the cancel's terminal fetch **books an exec delta or flips the slice's state**, the precomputed `place` is now **stale** — the slice may have just transitioned `usd1→usdt` (or vice-versa), so the old desired side/qty no longer applies and blindly placing it could open a wrong-side order or double-commit. v3 records such slices in `aborted` and **drops the paired place this tick**; `reconcile_orders` re-derives the desired set from fresh state on the **next** tick. The apply loop therefore **never trusts a precomputed action pair across a state change** — it re-derives. RED: `test_cancel_books_fill_before_clear_cancel_first`, `test_stale_place_aborted_after_cancel_flips_state`.

Cancels run before places per slice (a BUY locks cash; cannot place a new BUY before cancelling the old without `InsufficientFunds`). Cancel-first leaves a one-tick window with no resting order — acceptable; next reconcile re-places.

## A4. Real fills drive slice transitions (replacing `evaluate_fills`)

3a uses **REST polling** (no private WS — N3 deferred). One cheap `fetch_open(symbol)` per poll tick; any order that disappeared from open → `fetch_order_state` (open-then-terminal) for the final.

```
def poll_fills(self, now, client=None):
    assert self._r1_ok, "poll_fills before R1 gate"          # (✔F22/P2)
    if not self.maker_enabled: return
    if self.anchor is None:    return                        # (✔F19/P2)
    client = client or self.order_client
    for i, s in enumerate(self.slices):
        if not s["order_id"] and not s["order_link_id"]:     # (✔C-P1#11) poll when EITHER is set;
            continue                                          #   skip ONLY when BOTH absent (crash-after-place recovery)
        st = client.fetch_order_state(self.symbol, s["order_id"], link_id=s["order_link_id"])
        if st["status_class"] == "postonly_rejected":
            self._note_reject(i); self._clear_slice_order(i); self._persist_durable_or_halt(); continue
        self._apply_exec_delta(i, st, now)                   # shared helper: guards None/NaN, books delta, flips state
        self._persist_durable_or_halt()
```

```
def _apply_exec_delta(self, i, st, now) -> bool:
    """Shared by poll_fills, cancel-rebook (✔C-P0#3), and resume (✔F14). Returns True if it
    booked any exec delta OR flipped state (used by ✔C-P0#4 to abort a stale place)."""
    s = self.slices[i]
    filled, total = st["filled"], s["order_qty"]
    # (✔F20/P2) guard None AND non-finite on BOTH operands before the subtraction:
    if filled is None or total is None or not (math.isfinite(filled) and math.isfinite(total)):
        return False                                          # skip; re-poll next tick
    exec_delta = filled - s["filled_qty"]                     # new exec since last observation
    changed = False
    if exec_delta > 0:
        self._apply_exec(i, st["side"], exec_delta, st["avg"], now)
        s["filled_qty"] = filled; changed = True
    remaining = total - filled
    if remaining <= EPS_LOT:                                  # FULLY filled
        self._flip_state(i); self._clear_slice_order(i); changed = True  # transition (parity, ✔F18/P2)
    return changed
```

```
def _cancel_to_terminal(self, order_id, link_id, now, slice_idx=None, client=None) -> dict:
    """(✔R2-P0) Cancel an order, then POLL fetch_order_state until status_class is TERMINAL
    (filled|cancelled|rejected). Bybit can return PendingCancel / still-'open' right after a
    cancel, and the resting leaves CAN still fill in that window -> we MUST NOT clear local
    state while status_class=='open'. Bounded backoff; if it never reaches terminal ->
    _halt_operator_reconcile (fail-closed, never clear on an unknown outcome). When slice_idx
    is given, books the FINAL cumExecQty delta to that slice (flips/clears) ONLY after terminal
    is confirmed; returns the terminal st dict with a 'changed' flag.
    `client` is threaded from the caller (✔R3-P1) so the SAME injected client used for
    matching/balance is used here — never silently falls back to a different one (test seam)."""
    client = client or self.order_client                      # (✔R3-P1) honor injected client
    client.cancel(self.symbol, order_id, link_id=link_id)
    st = None
    for backoff in CANCEL_POLL_BACKOFFS:                      # e.g. (0,.25,.5,1,2)s — bounded, config
        st = client.fetch_order_state(self.symbol, order_id, link_id=link_id)
        if st["status_class"] != "open":                     # terminal: filled|cancelled|rejected
            break
        self._sleep(backoff)                                  # PendingCancel/open -> keep polling
    else:
        self._halt_operator_reconcile(f"cancel never reached terminal: {link_id or order_id}")
        return {**st, "changed": False}                       # halt raises; unreached
    changed = False
    if slice_idx is not None:                                 # book ONLY after terminal confirmed
        changed = self._apply_exec_delta(slice_idx, st, now)  # final cumExecQty; flips if leaves<=EPS
        self._clear_slice_order(slice_idx); self._persist_durable_or_halt()
    return {**st, "changed": changed}
```

```
def _apply_exec(self, i, side, dq, px, now):
    s = self.slices[i]
    if side == "sell":
        dq = min(dq, s["qty"])           # min(calc, available) -> never overshoot/flip
        s["qty"]          -= dq
        s["cash"]         += dq * px
        s["sell_proceeds"]+= dq * px     # (✔C-P1#8) persistent blended-avg basis
        s["qty_sold"]     += dq          # (✔C-P1#8)
        s["sell_px"]       = px          # last-sell, display only (NOT used for realized)
    else:  # buy / rebuy — realized booked here, BEFORE cash is reduced (✔C-P1#8)
        if s["qty_sold"] > 0:                                 # blended avg over ALL partial sells
            avg_sell = s["sell_proceeds"] / s["qty_sold"]
            self.realized_capture += (avg_sell - px) * dq     # exact under any mix of partial-sell prices
        s["qty"]  += dq
        s["cash"]  = max(0.0, s["cash"] - dq * px)
    self._log_event(now, side, i, px, dq)   # SINGLE persistence point per fill (A9 / F10)
```

**REALIZED_CAPTURE under multi-price partial sells (✔F6/P1, ✔C-P1#8 — v2 open note RESOLVED):** v1 booked `(s["sell_px"] − px)·dq`, but `sell_px` is only the *last* sell price, mispricing a slice whose proceeds accrued across **multiple** partial-sell prices. v2 flagged the fix but left the formula on an unbacked `cash`-shorthand (which degenerates to ~0 on a full-cash rebuy) as an open IMPLEMENTER NOTE. **v3 closes it with real persistent fields:** `sell_proceeds` and `qty_sold` accumulate across every partial sell, so the blended `avg_sell = sell_proceeds / qty_sold` is exact, and `realized += (avg_sell − px)·dq` is booked **before** `cash` is reduced. **Reduction-to-paper (must be confirmed before GREEN, ✔R3-P2):** for a single-price full cycle — sell `q` at `R` (⇒ `sell_proceeds=R·q`, `qty_sold=q`, `avg_sell=R`), then a full rebuy spends the proceeds at `B`, so the rebuy delta is `dq = nq = cash/B = R·q/B` (NOT `q` — proceeds `R·q` buy back `R·q/B > q` base) — this yields `realized += (avg_sell−B)·nq = (R−B)·nq`, **identical** to paper's `(sell_px−B)·nq`. RED: `test_realized_uses_persistent_sell_proceeds`, `test_realized_capture_exact_under_multi_price_partial_sells`, plus the F18 parity test.

**`_flip_state` parity (✔F18/P2):** on a full fill, `_flip_state(i)` must reset the **exact same fields** the paper `evaluate_fills` resets at a transition — entry/sell_px and the cash/qty zeroing for the leg being left (a completed SELL: `cash` holds proceeds, `qty→0`, `state="usdt"`, `entry=None`, **keep** `sell_proceeds`/`qty_sold` as the basis for the upcoming rebuy; a completed BUY/rebuy: `qty` holds coins, `cash→0`, `state="usd1"`, `entry=B`, **reset** `sell_proceeds=0.0`, `qty_sold=0.0` — the cycle is closed). Specifying identical field resets is what makes a full sell→full buy maker cycle produce **identical `realized_capture` to paper** for the same prices (RED: `test_flip_state_resets_same_fields_as_evaluate_fills`, `test_full_cycle_maker_realized_capture_parity_with_paper`).

## A4a. State-switched readers under partial fills (✔F2/P0 — all 3 personas) + valuation split by leg (✔C-P1#12)

During a partial fill a slice holds **both** real base (`qty>0`) and proceeds (`cash>0`) while `state` is still its pre-flip value. Every reader that branches on `state` therefore under-reports. Two distinct fixes:

**(1) Per-slice readers value both legs (✔F2/P0):**

| Reader | v1 (state-switched, WRONG under partial) | v3 (both legs, any slice) |
|---|---|---|
| `_usd1_qty` (carry base) | `Σ qty for state=="usd1"` | `Σ qty across ALL slices` |
| `_slice_value` (per-slice equity) | `qty*mark` if usd1 else `cash` | `qty*mark + cash` for ANY slice |
| `_local_summary` (R1 reconcile) | base=`Σqty(usd1)`, quote=`Σcash(usdt)` | base=`Σqty(all)`, quote=`Σcash(all)` |

**(2) `status_doc` aggregation split by LEG, not by slice-state (✔C-P1#12 — refines v2):** generalizing `_slice_value` alone is insufficient because `status_doc` (engine.py:643-655) separately accumulates `usd1_value`/`usdt_value` **keyed on `s["state"]`** — a mid-partial slice dumps its *entire* `qty*mark+cash` into one bucket by its stale state, mis-stating both legs. v3 stops keying valuation on state and computes, across **ALL** slices independent of state:

```
base_value  = Σ s["qty"] * mark          # the carry-bearing leg, every slice
quote_value = Σ s["cash"]                # the parked leg, every slice
total_value = base_value + quote_value
# display fields derive from these (e.g. usd1_value:=base_value, usdt_value:=quote_value, usd1_pct from them)
```

So a partial slice contributes its base to `base_value` and its proceeds to `quote_value` simultaneously — correct. RED: `test_status_base_quote_value_independent_of_state`.

**Safety of the generalization:** on a fully-settled slice the extra term is zero (a `usd1` slice has `cash==0`; a `usdt` slice has `qty==0`), so v3 equals v1 on all clean states — a strict superset that additionally captures the transient mixed state. Consequences fixed:
- **Carry:** a resting SELL that doesn't fill keeps base in `qty` and **keeps earning carry**; partial residual is counted (✔F2).
- **Valuation:** `status_doc` no longer mis-buckets a mid-partial slice (✔C-P1#12).
- **R1 restart:** `_local_summary` reports both legs, so a **mid-partial restart no longer false-refuses** when the exchange shows both base residual + quote proceeds (✔F2).

**Carry correctness (subtle, ✔F4/P1):** because `_usd1_qty` now reflects partial base, the snapshot must be taken with `accrue(now)` running **before** `poll_fills` mutates `qty` (A4b) so the integer-hour snapshot reflects the top-of-hour holding — parity with the backtest.

## A4b. Run-loop wiring — exact call sites + cadence (✔F4/P1, run-order corrected ✔C-P0#5)

Pin where the maker code runs so it can never silently not-run or double-run:

- **`_handle(d, now)`** keeps `self.accrue(now)` as its **first** statement (engine.py:919 — preserves the top-of-hour snapshot). When `maker_enabled`, the orderbook/publicTrade branches **skip** `_maybe_deploy()` and `evaluate_fills(now)` (real fills + balance-seeded position replace them) — explicit `if not self.maker_enabled:` guards, not implicit. RED: `test_evaluate_fills_and_maybe_deploy_skipped_when_maker_enabled`.
- **`_tick(now)`** runs `maker_step(now)` in the **throttled** branch (the existing `now - self.last_status >= STATUS_EVERY` gate, == 12s) so order churn matches the status cadence, not every WS frame. Inside the throttled branch the order is **`accrue(now)` → `maker_step(now)` → status write**, and `maker_step` itself is **`poll_fills(now)` THEN `reconcile_orders(now)`** (✔R3-P0 — corrects v4's reversed order), with `accrue` already done. **Why poll BEFORE reconcile (✔R3-P0):** a slice whose resting order filled/cancelled has left the open book; if `reconcile_orders` ran first, `match_live_orders` would find no open order for that slice, treat it as "no live order", and **place a NEW order (bumping `order_gen`/`order_link_id`) before the old order's terminal `cumExecQty` was ever booked** — losing the fill and double-exposing. `poll_fills` first terminal-syncs every slice (books fills, flips state, clears completed orders) so `reconcile_orders` computes `desired` from already-correct state. **Defence-in-depth:** `reconcile_orders` additionally terminal-syncs any slice whose persisted `order_id`/`order_link_id` is **absent from the current open set** (`fetch_order_state`→`_apply_exec_delta`) **before** it may emit a `place` for that slice — so an order vanishing in the sub-tick window between poll and reconcile still cannot be overwritten unbooked. Carry is snapshotted before any fill mutates `qty` (✔F4/P1). RED: `test_maker_step_polls_before_reconciles` (✔R3-P0), `test_reconcile_terminal_syncs_vanished_order_before_place` (✔R3-P0), `test_maker_step_invoked_on_armed_path`, `test_carry_accrues_before_poll_fills_across_hour`.
- **`run()` ordering — CORRECTED (✔C-P0#5):** `_maybe_gate()` → `bootstrap()` → **`resume_reconcile_orders(self._r1_open_orders)`** → recv loop. The seeding step is **no longer a separate stage between gate and bootstrap** — it has moved **INSIDE** the gate, *before* `reconcile()` decides (A6a). `_maybe_gate` sets `self._r1_ok` and stores `self._r1_report` + `self._r1_open_orders`; `resume_reconcile_orders` reuses that **same** open-orders list (no refetch, ✔F23). `reconcile_orders` must **not place for a slice until resume has resolved that slice's in-flight `link_id`** (✔F14/P1).

## A5. Persistence (crash-safe order↔slice map)

- New order fields ride through `_state_dict` (slices serialized wholesale). Schema **v=2**; `_maybe_resume` accepts v2 and **migrates v1** (inject defaults: `order_id=None,…,filled_qty=0.0,order_gen=0,reject_streak=0,sell_proceeds=0.0,qty_sold=0.0`). Type-checks extended (engine.py:402-411): `order_id` str|None, `order_side` ∈ {buy,sell,None}, `filled_qty` float, `order_gen` int, `sell_proceeds` float, `qty_sold` float. Atomic fail-to-fresh preserved for paper; **gated for armed-maker** (A9).
- **Persist-intent-before-place invariant** (extends "snapshot ≥ event log", engine.py:561-567): persist `order_link_id`+`order_gen` (the INTENT) **before** the place call; persist `order_id` the instant the exchange acks. A crash never orphans a live resting order.
- **Restart reconciliation `resume_reconcile_orders(open_orders, client)`** (after the R1 gate, before the recv loop). **Single-owner split (✔F23/P2, ✔C-P0#5):** `reconcile.py` already decided proceed/refuse from balances + expected orders; this method only performs the **side-effects**, on the **same `open_orders` list the gate stored** (no refetch). Matching uses the pure `match_live_orders` (link_id → id → approx). For each open order:
  - (a) known + expected → re-link.
  - (b) ours (`sca-*`) but not expected → **orphan → cancel + log**.
  - (c) non-`sca` order in the dedicated subaccount → **refuse** (subaccount must be dedicated).
- **Lost-fill + uncertain-retry recovery (✔F14/P1):** for **EVERY** slice that has `order_link_id` set — **regardless** of whether `order_id` was durably persisted — call `fetch_order_state(link_id=...)` (open-then-terminal) and apply `_apply_exec_delta` **before** resuming, so a fill that completed *while the engine was down* (and whose id may never have been recorded) is recovered. Before bumping `order_gen` for a slice with a persisted `order_link_id` but `order_id=None`, **first `fetch_order_state(link_id)` to confirm the prior order is truly absent** — `fetch_open` is **non-authoritative for absence** (an order can be open but missing from a paged/stale snapshot; and a terminal Filled/Cancelled order won't appear in open at all). RED: `test_crash_after_place_before_id_persist_recovers_fill_while_down`, `test_uncertain_retry_fetches_state_never_two_live_orders`.

## A6. Gates (3a keeps every existing gate + adds testnet-only)

3a real placement is reachable only when ALL hold (each an independent logical flag):
1. `mode == "live"` AND `LIVE_TRADING_CONFIRM == "yes"` AND keys present → `live_authorization` → `self.armed`.
2. R1 reconcile gate passes (now maker-aware) → sets `self._r1_ok = True` (✔F22/P2). `reconcile_orders`/`poll_fills` **assert `_r1_ok`** at entry (defence-in-depth).
3. `resolve_testnet()` true (sandbox). **`MakerOrderClient` refuses to even construct on mainnet (✔F21/P2)** and `place_postonly` independently refuses on mainnet — two layers.
4. **`resolve_maker_enabled()` true (✔C-P1#14)** — the explicit rollback knob (`env > runtime.maker_enabled > default false`). `maker_enabled = armed and resolve_testnet() and resolve_maker_enabled()`. Off ⇒ paper path, zero behavior change.

**`engine.py:844` `fresh_deploy` refusal is UNCONDITIONAL and STAYS so (✔F22/P2 — corrects v1).** v1's A6 implied "3a enables the testnet real path" by loosening the gate — it does **not**. On **both** mainnet and testnet, a reconcile-approved `fresh_deploy` is still refused. Testnet first-deploy is reached **only** via the documented "seed local state → `proceed`" escape hatch (engine.py:847-850), see A6a. Paper never builds `MakerOrderClient`.

## A6a. Armed-maker initial position seeding — INSIDE the gate, before reconcile decides (✔F3/P1 + ✔F22/P2 + ✔C-P0#5 + ✔C-P1#15)

**Problem v1 missed:** an armed-maker testnet start with no local state can never exercise the lifecycle. A config-`alloc` simulated `_deploy` creates all-`usd1` slices wanting SELLs at `anchor+rung`; but the testnet account doesn't actually *hold* that USD1, so `avail_base≈0` floors every SELL qty to 0 → **no orders ever placed** → nothing testable. A USDT-funded testnet should instead start with **`usdt`-state slices wanting BUYs** (real `avail_quote`) → real places.

**Problem v2 missed (✔C-P0#5):** v2 ran seeding as a step *between* the gate and bootstrap — i.e. **after** `reconcile()` had already decided against the **un-seeded** (empty) local summary. With no local state, `reconcile()` would see local=empty vs exchange=funded and refuse/`fresh_deploy` *before* seeding ever ran. The seed must therefore happen **before** the decision.

**Fix (✔C-P0#5):** move `_seed_slices_from_balance(bal)` **INTO `_reconcile_or_refuse`**, executed **after** the liability guard but **before** the `reconcile(self._local_summary(), …)` call — so the **seeded** local summary is what `reconcile()` compares. On the armed-maker (testnet) path only, when there is no local state: USDT holdings → `usdt`-state slices (cash, wanting BUYs); USD1 holdings → `usd1`-state slices (qty, wanting SELLs); mark `resumed=True`. `reconcile()` then compares the seeded summary against the same balance → returns **`proceed`** (never `fresh_deploy`). Scoped by `resolve_testnet()` so it is **impossible on mainnet** (no seeding → empty local → refuse / fresh_deploy-refused). The gate then stores `self._r1_report` + `self._r1_open_orders` for resume to reuse.

**Seeding safety — refuse a mixed/ambiguous balance (✔C-P1#15):** `_seed_slices_from_balance` seeds **ONLY** when **both** hold: (a) there are **NO open orders** on the account, and (b) the balance is a **clean single-side** position above `tol` (essentially all-USDT *or* all-USD1, the other side ≤ dust `tol`). A **mixed** balance (both base and quote materially present) or a balance with pre-existing open orders is **ambiguous lost state** → **REFUSE** (never silently legitimize it, even on testnet — guessing the intended slice split could mis-seed the lifecycle). RED: `test_seed_refuses_mixed_balance`, `test_seed_before_reconcile_decides`.

RED: `test_armed_maker_testnet_start_emits_place_action` (≥1 `place` action after seed-in-gate → proceed → reconcile_orders).

## A7. Testnet plumbing

`MakerOrderClient(testnet=resolve_testnet())` → `set_sandbox_mode(True)`. **No split-brain (✔F13/P1):** `_reconcile_or_refuse` passes `testnet=resolve_testnet()` into `BybitPrivateClient`, and the maker client reads the **same** resolver, so the R1 gate and the order client are **provably same-venue**. RED: `test_both_clients_get_identical_testnet_no_split_brain`. Unit tests exercise the full lifecycle via an injected fake ccxt module (place → partial → full; place → postonly-reject → cooldown; place → cancel-books-residual; place → cancelled/rejected terminal; 429 → backoff; dup link_id {170141,12141,30001} → idempotent) with **zero network** (mirrors `test_bybit_client.py` `_mk(**over)` seam). A **manual testnet smoke** (real `sca live` against Bybit testnet, full lifecycle incl partials) is the **3b entry gate** — documented, not a merge blocker.

## A8. Error / edge handling summary

| Edge | Handling |
|---|---|
| PostOnly reject | returned `canceled`/`rejected`+`EC_PostOnlyWillTakeLiquidity` OR raised `InvalidOrder` → `postonly_rejected` → clear slice order + **per-slice cooldown** (A8a, F9) |
| min-size order | dropped in `desired_orders` (F19); defence: `InvalidOrder`→`too_small`→logged-skipped, never retried (F19) |
| over-cap notional | clamped/dropped in `desired_orders` AND hard-asserted in `place_postonly` (F11) |
| order-state retrieval | open-by-linkid (`fetch_open_orders`), absent ⇒ terminal-by-linkid (`fetch_canceled_and_closed_orders`); never `fetch_closed_order` (Filled-only) (✔C-P0#1) |
| cancelled / rejected terminal | observable via `fetch_canceled_and_closed_orders` `orderStatus`+`rejectReason` — a cancelled-partial / postonly-reject is never invisible (✔C-P0#2) |
| create returns no fills | always re-poll via `fetch_order_state`; success asserted from `filled`, never absence |
| Partial fill | apply `exec_delta`, keep remainder resting, flip state only at `remaining<=EPS_LOT`; never amend a partial (F8) |
| cancel with residual | **cancel FIRST**, then `_cancel_to_terminal` POLLS until terminal (never clear while `open`/PendingCancel, ✔R2-P0), book final `cumExecQty` delta, THEN clear (✔C-P0#3) — cancel never drops a fill |
| unattributable / ambiguous order | cancel-to-terminal with no slice; any executed qty ⇒ `_halt_operator_reconcile`; clean cancel ⇒ log; never clears a guessed slice (✔R2-P1) |
| stale place after state-flip | a cancel that books a delta / flips state aborts the paired precomputed place this tick; re-derive next tick (✔C-P0#4) |
| amend | qty-ONLY, NO postOnly/timeInForce (ccxt amend schema has neither; exchange preserves TIF), re-poll to verify (✔C-P1#7) |
| dup clientOrderId | retCode 170141 / 12141 / 30001 (and "duplicate"/"repeated" msg) ALL → idempotent fetch-state path (✔C-P2#16) |
| None / NaN filled-total | guarded on BOTH operands before subtraction; non-finite/None → skip, re-poll (F20) |
| anchor is None | `reconcile_orders`/`poll_fills` no-op (F19) |
| Overshoot | sell/buy qty `= min(calculated, available)` + aggregate pool bound (F16); avail = free + own-locked, new placement validated post-cancel (✔C-P1#13) |
| 429 | exp backoff 1→2→4→cap30s, honor Retry-After, bounded |
| Disconnect/restart | `resume_reconcile_orders` (gate-fetched list, no refetch): match by link_id→id→approx, cancel orphans, recover down-time fills, uncertain-retry confirms absence (F14, F23, C-P0#5) |
| mixed/ambiguous seed balance | REFUSE — never legitimize lost mixed state, even on testnet (✔C-P1#15) |
| Persist failure (real orders) | **fail-closed** `_persist_durable_or_halt`; on exhaustion → cancel-all + halt (F10) |
| process exit / SIGTERM | try/finally + signal handler → cancel ALL resting orders, persist cleared map (F12) |

## A8a. PostOnly-reject cooldown — anti-livelock (✔F9/P1)

The anchor moves only **hourly**, but `_tick` runs `maker_step` every ~12s. A naive "reject → re-quote next tick" therefore **hot-loops the same doomed rung up to ~1h** until the anchor moves. Fix: **per-slice cooldown** keyed off the anchor / top-of-book:
- Once a slice's place is `postonly_rejected`, **do not re-attempt that slice** until the **anchor changes** (a new 1h close) OR top-of-book crosses the rung (the price moved enough that the rung would now rest). `_in_cooldown(slice_idx, desired)` gates the place in `reconcile_orders`.
- Count **consecutive** rejects per slice (`reject_streak`); on success `_reset_reject`. When any slice's streak crosses a configured threshold → **halt/alert** (cancel-all + refuse), surfacing a stuck rung rather than spinning silently.
- RED: `test_postonly_reject_slice_cooldown_until_anchor_change`, `test_consecutive_postonly_rejects_trip_halt_threshold`.

## A9. Fail-closed persistence stance (D10) + single persistence point (✔F10/P1)

Paper keeps fail-OPEN (`save_state` swallows OSError, continues). **Armed-maker is fail-CLOSED:**
- On the maker path, fill logging routes through **one** primitive, `_persist_durable_or_halt()`, which retries the atomic write with bounded backoff; if it cannot persist it **stops placing, cancels resting orders (`_cancel_all_resting`), and refuses** rather than continue with an in-memory-only fill.
- **Single persistence point per fill (✔F10/P1):** a maker fill persists in exactly one place (`_log_event` → fail-closed when `maker_enabled`), never double-written from both the fill path and the status path — so an OSError can't leave a half-written fill. `_log_event`'s persist becomes **fail-closed when `maker_enabled`** (paper still fail-open).
- The corrupt/missing-state path on armed-maker is **gated behind `resume_reconcile_orders`** (exchange truth), never a silent fresh deploy.
- RED: `test_maker_fill_persist_oserror_halts_cancel_all`, `test_maker_fill_single_persist_point`.

## A10. Kill switch / cancel-all-on-exit (✔F12/P1 — owner-required, absent in v1)

Resting maker orders MUST NOT survive the process. Wrap the recv loop in **`try/finally`**: on **ANY** exit — deadline reached, exception, `KeyboardInterrupt`, or `SIGTERM` — `_cancel_all_resting(client)` cancels **every persisted resting order**. **It routes each one through `_cancel_to_terminal` (✔R3-P1)** — NOT a blind cancel-and-clear — so a fill that lands during shutdown is polled to terminal and **booked** before the slice's order state is cleared, honoring the same "never clear before terminal truth" rule as the live path. If a terminal state cannot be confirmed for some order within the bounded retries, it **fails closed** (leaves that slice's order state intact + surfaces the halt) rather than persisting a cleared map over an unknown outcome. Only after every order is terminal-resolved does it persist the cleared map and log. A **signal handler** for `SIGINT`/`SIGTERM` routes to the same path so a `docker stop`/Ctrl-C can't leave live orders dangling or drop a shutdown-window fill. RED: `test_run_exit_cancels_all_resting_orders`, `test_sigterm_triggers_cancel_all`, `test_cancel_all_books_shutdown_window_fill_before_clear` (✔R3-P1).

---

# PART B — IMPLEMENTATION PLAN (TDD, ordered, independently testable)

Each task ≤3 files, RED tests first. Run: `PYTHONPATH=src python3 -m pytest tests/ -q`. No conftest — every new test file repeats the `sys.path.insert(0, …/src)` line and uses `tmp_path`/`monkeypatch` + module-level helpers + UPPER_CASE canned dicts. Tests carry `(✔F#)` / `(✔C-P#)` cross-refs in `# --- section ---` banners. **Sequential order (deps noted); Tasks 4/5/6 all touch `engine.py` → never dispatch in parallel.**

### Task 1 — pure precision + desired/diff/match core (`order_recon.py`)  [deps: none]
**Files:** `src/sca/live/order_recon.py` (new), `tests/test_order_recon.py` (new).
**RED tests:**
- `test_quantize_buy_floors_never_crosses_up` / `test_quantize_sell_ceils_never_crosses_down`
- `test_quantize_qty_floors_to_lot`
- `test_desired_usd1_is_sell_at_rung_qty_min_avail` / `test_desired_usdt_is_buy_at_rebuy_qty_cash_over_price`
- `test_desired_quantizes_qty_with_lot_param` (✔F17), `test_qty_tol_defaults_half_lot` (✔F17)
- `test_within_one_tick_bucket_zero_touch` (✔F15), `test_one_bp_anchor_move_reprices_affected_rungs` (✔F15)
- `test_diff_unchanged_prices_all_leave_zero_touch`
- `test_diff_price_move_is_cancel_then_place`
- `test_diff_qty_down_amends_unfilled_qty_up_or_partial_cancel_recreate` (✔F8)
- `test_diff_compares_remaining_to_remaining` (✔F8 — `Live.qty` = leaves)
- `test_live_has_filled_qty` (✔C-P1#9 — `Live.filled_qty` from cumExecQty; diff branches on it)
- `test_match_live_orders_returns_ambiguous_in_unattributed` (✔C-P1#10, ✔R2-P1 — link_id→id→unambiguous-approx into MATCHED; no-slice / >1-candidate / stale `sca-*` into the UNATTRIBUTED list, never a guessed slice; replaces v2's `test_diff_match_*`/`test_diff_ambiguous_*` + round-1's `*_refuse_*`)
- `test_unattributed_order_with_fill_halts_operator_reconcile` (✔R2-P1 — unattributable order with cumExecQty>0 ⇒ `_halt_operator_reconcile`; clean cancel ⇒ log only, no slice touched)
- `test_desired_aggregate_avail_pool_decrements_bounded` (✔F16)
- `test_desired_clamps_or_drops_above_max_order_usd` (✔F11)
- `test_desired_drops_below_min_qty_and_min_cost` (✔F19)
**Done when:** all green, 100% line+branch on `order_recon.py`.

### Task 2 — maker order client + read-client shape (`orders.py`, `bybit_client.py`)  [deps: Task 1]
**Files:** `src/sca/live/orders.py` (new), `tests/test_orders.py` (new), `src/sca/live/bybit_client.py` (1-line read-only `normalize_order` field, ✔C-P1#6), `tests/test_bybit_client.py` (shape-test assert only). *(The `bybit_client` touch is a single read-only field + its assertion — no logic, no order method — so the ≤3-files spirit holds; the no-order-method invariant `:133` is untouched.)*
**RED tests:**
- `test_place_postonly_builds_correct_ccxt_call`, `test_place_passes_snapped_price_not_ccxt_round`
- `test_place_postonly_asserts_max_order_usd_raises` (✔F11)
- `test_postonly_reject_returned_order_classified_not_error`, `test_postonly_reject_raised_exception_classified_not_error`
- `test_min_size_invalid_order_classified_too_small_not_retried` (✔F19)
- `test_dup_linkid_retcodes_all_idempotent` (✔C-P2#16 — 170141 AND 12141 AND 30001 all → fetch-state path)
- `test_fetch_state_open_by_linkid_then_terminal_by_linkid` (✔C-P0#1 — `fetch_open_orders(params orderLinkId)` first; absent ⇒ `fetch_canceled_and_closed_orders(params orderLinkId)`)
- `test_fetch_terminal_state_covers_cancelled_and_rejected` (✔C-P0#2 — cancelled-partial / postonly-reject row observable via terminal `orderStatus`+`rejectReason`)
- `test_fetch_state_never_calls_filled_only_closed_path` (✔C-P0#1/#2 — never `fetch_order`/`fetch_closed_order`/`fetch_closed_orders`)
- `test_fetch_state_asserts_filled_finite`
- `test_fetch_state_by_link_id_only_when_id_absent` (✔F14)
- `test_fetch_open_exposes_client_order_id` (✔F5 — full open list carries clientOrderId)
- `test_get_open_orders_normalizes_keeps_client_order_id` (✔C-P1#6 — read-client shape; the deliberate `:174` shape change)
- `test_amend_qty_only_no_tif_no_postonly` (✔C-P1#7 — amend payload carries qty only; NO postOnly/timeInForce; supersedes v2's reassert-postOnly test)
- `test_amend_refuses_price_change` (✔F8/✔C-P1#7 — price arg ⇒ raise; re-price is cancel+recreate)
- `test_amend_refuses_partially_filled_order` (✔F8 — routes to recreate)
- `test_amend_total_qty_leaves_semantics` (✔F8 — leaves = newQty−cumExec; re-poll verifies)
- `test_rate_limit_backoff_retries_then_succeeds` (+ cap, bounded)
- `test_insufficient_funds_skips_not_crash`
- `test_place_postonly_refused_on_mainnet` (✔F21 — place-level)
- `test_maker_client_refuses_construction_on_mainnet` (✔F21 — ctor-level hard raise)
**Seam:** fake ccxt module (mirror `_mk(**over)`); `FakeExchange` records `.create_order/.edit_order/.cancel_order/.fetch_open_orders/.fetch_canceled_and_closed_orders` + returns canned V5 dicts (open row with `cumExecQty`/`leavesQty`; terminal Filled; terminal Cancelled+`rejectReason`). **Done when:** green, ~100% coverage (core trading path — Lee red line); `test_client_exposes_no_order_methods` (`:133`) still green.

### Task 3 — maker-aware R1 reconcile + config resolvers (`reconcile.py`, `config.py`, `strategy.yaml`)  [deps: none — pure + config]
> May split into 3a (`reconcile.py` + `test_reconcile.py`) and 3b (`config.py` + `strategy.yaml` + `test_config_runtime.py`) to honour ≤3 files.
**Files:** `src/sca/live/reconcile.py` (mod), `src/sca/config.py` (mod — `resolve_testnet` + `resolve_maker_enabled`), `config/strategy.yaml` (mod), extend `tests/test_reconcile.py` + `tests/test_config_runtime.py`.
**RED tests:**
- `test_reconcile_expected_maker_orders_proceeds` (✔F1 — orders no longer auto-anomaly)
- `test_reconcile_orphan_order_refuses`
- `test_reconcile_empty_expected_preserves_taker_refuse` (13 existing tests stay green)
- `test_reconcile_balance_still_checked_with_orders`
- `test_local_summary_sums_base_and_quote_across_all_slices` (✔F2)
- `test_reconcile_proceeds_on_mid_partial_restart` (✔F2)
- `test_reconcile_decides_resume_applies_ownership_split` (✔F23 — reconcile returns decision, no side-effects)
- `test_resolve_testnet_env_over_runtime_over_default` (✔F13)
- `test_live_testnet_redirects_to_runtime_testnet` (✔F13 — single source, no split-brain knob)
- `test_resolve_maker_enabled_precedence` (✔C-P1#14 — env > runtime.maker_enabled > default false)
**Done when:** green; existing reconcile/config tests unchanged.

### Task 4 — engine maker fill driver: transitions + reconcile-apply + poll  [deps: Tasks 1,2,3]
**Files:** `src/sca/live/engine.py` (mod), `tests/test_engine_maker_fills.py` (new).
**RED tests:**
- `test_full_sell_fill_flips_usd1_to_usdt_clears_order`
- `test_full_buy_fill_books_realized_capture`
- `test_realized_uses_persistent_sell_proceeds` (✔C-P1#8 — avg_sell = sell_proceeds/qty_sold, booked before cash reduced)
- `test_realized_capture_exact_under_multi_price_partial_sells` (✔F6/✔C-P1#8)
- `test_partial_sell_updates_qty_cash_proceeds_keeps_state_usd1`, `test_partial_then_full_completes_transition`
- `test_cancel_books_fill_before_clear_cancel_first` (✔C-P0#3 — cancel FIRST, terminal re-poll books a fill landed during cancel)
- `test_cancel_polls_through_pending_cancel_until_terminal` (✔R2-P0 — `_cancel_to_terminal` keeps polling while `status_class=='open'`/PendingCancel, never clears until terminal; bounded-retry exhaustion ⇒ `_halt_operator_reconcile`)
- `test_unattributed_order_with_fill_halts_operator_reconcile` (✔R2-P1 — engine: an unattributed order with executed qty halts; a clean stray cancel touches no slice)
- `test_stale_place_aborted_after_cancel_flips_state` (✔C-P0#4 — cancel that books delta/flips state drops the paired place this tick)
- `test_flip_state_resets_same_fields_as_evaluate_fills` (✔F18 — incl. reset sell_proceeds/qty_sold on rebuy completion)
- `test_full_cycle_maker_realized_capture_parity_with_paper` (✔F18 — same prices ⇒ identical realized)
- `test_none_or_nonfinite_filled_total_guarded_skips` (✔F20)
- `test_sell_qty_capped_at_available_no_overshoot`
- `test_avail_uses_free_plus_own_locked` (✔C-P1#13 — sizing pool = free + own-resting-leaves; new placement validated post-cancel)
- `test_carry_sums_base_across_all_slices` (✔F2 — `_usd1_qty`)
- `test_status_base_quote_value_independent_of_state` (✔C-P1#12 — base_value=Σqty·mark, quote_value=Σcash over ALL slices)
- `test_status_doc_valuation_under_partial_fill` (✔F2 — `_slice_value` per-slice both legs)
- `test_reconcile_orders_noop_when_anchor_none` / `test_poll_fills_noop_when_anchor_none` (✔F19)
- `test_poll_when_only_link_id_present` (✔C-P1#11 — poll a slice with link_id set but order_id=None; skip only when BOTH absent)
- `test_postonly_reject_slice_cooldown_until_anchor_change` (✔F9), `test_consecutive_postonly_rejects_trip_halt_threshold` (✔F9)
- `test_reconcile_orders_asserts_r1_ok` / `test_poll_fills_asserts_r1_ok` (✔F22)
- `test_reconcile_zero_actions_when_anchor_submove`, `test_reconcile_anchor_step_replaces_affected_orders`
**Seam:** injectable `client=` on `reconcile_orders`/`poll_fills`; real `PaperEngine` built, live fields set directly, `_r1_ok` set in fixtures; fake order client records calls + returns canned states. **Done when:** green + existing 130 unchanged.

### Task 5 — persistence v2 + crash-resume reconciliation + fail-closed  [deps: Task 4]
**Files:** `src/sca/live/persistence.py` (mod), `src/sca/live/engine.py` (resume validate/restore + `resume_reconcile_orders` + `_persist_durable_or_halt`), `tests/test_maker_persistence_resume.py` (new).
**RED tests:**
- `test_order_fields_roundtrip_v2` (incl. sell_proceeds/qty_sold), `test_v1_state_migrates_with_default_order_fields` (incl. sell_proceeds=0.0, qty_sold=0.0 — ✔C-P1#8), `test_resume_typecheck_rejects_bad_order_fields_fresh_start`
- `test_persist_intent_before_place` (link_id+gen on disk before network)
- `test_maker_fill_persist_oserror_halts_cancel_all` (✔F10 — fail-closed)
- `test_maker_fill_single_persist_point` (✔F10)
- `test_restart_matches_open_orders_relinks`, `test_restart_cancels_orphan_sca_order`, `test_restart_refuses_foreign_order_in_dedicated_account`
- `test_crash_after_place_before_id_persist_recovers_fill_while_down` (✔F14)
- `test_uncertain_retry_fetches_state_never_two_live_orders` (✔F14)
- `test_resume_uses_passed_open_orders_no_refetch` (✔F23/✔C-P0#5 — resume consumes the gate-stored list)
**Done when:** green; all save/load/resume paths synced; migration idempotent on reload.

### Task 6 — engine run-loop wiring + seed-in-gate + kill switch + lifecycle gate  [deps: Tasks 2,3,4,5]
**Files:** `src/sca/live/engine.py` (mod — `_handle`/`_tick`/`run` guards, `maker_step`, `_r1_ok`+`_r1_report`+`_r1_open_orders` set, seed-in-gate, signal/finally), `tests/test_engine_maker_runloop.py` (new).
**RED tests:**
- `test_evaluate_fills_and_maybe_deploy_skipped_when_maker_enabled` (✔F4)
- `test_maker_step_invoked_on_armed_path` (✔F4)
- `test_carry_accrues_before_poll_fills_across_hour` (✔F4)
- `test_seed_before_reconcile_decides` (✔C-P0#5 — seeding runs INSIDE the gate before `reconcile()`; seeded summary ⇒ proceed)
- `test_seed_refuses_mixed_balance` (✔C-P1#15 — mixed/ambiguous balance or pre-existing open orders ⇒ refuse, no seed)
- `test_armed_maker_testnet_start_emits_place_action` (✔F3 — seed-in-gate → proceed → place)
- `test_resume_uses_gate_fetched_open_orders` (✔C-P0#5 — run() passes `self._r1_open_orders`, no refetch)
- `test_fresh_deploy_still_refused_on_testnet_and_mainnet` (✔F22 — gate not loosened)
- `test_both_clients_get_identical_testnet_no_split_brain` (✔F13)
- `test_maker_enabled_off_falls_back_to_paper_path` (✔C-P1#14 — resolve_maker_enabled=false ⇒ evaluate_fills path)
- `test_run_exit_cancels_all_resting_orders` (✔F12), `test_sigterm_triggers_cancel_all` (✔F12)
- `test_armed_maker_only_when_live_confirm_keys_testnet_maker` (logical-flag gate, 4 conditions)
- `test_paper_mode_never_builds_order_client_still_simulates` (130-test safety)
**Done when:** green + existing 130 unchanged.

### Task 7 — governance docs (D11 + R1 note)  [deps: none — doc-only, no RED test]
**Files:** `docs/decisions.md` (add **D11 — execution model re-locked TAKER→MAKER, supersedes R1 TAKER lock**), `docs/live-bybit-readonly-r1-plan.md` (note: Phase-3 execution model is now MAKER; the §line "trading via taker … applies in Phase 3" is superseded by D11). **(✔F1/P0).** No code test; the code half is covered by Task 3's maker-aware reconcile tests.

### Full file list
**New:** `src/sca/live/order_recon.py`, `src/sca/live/orders.py`, `tests/test_order_recon.py`, `tests/test_orders.py`, `tests/test_engine_maker_fills.py`, `tests/test_engine_maker_runloop.py`, `tests/test_maker_persistence_resume.py`.
**Modified:** `src/sca/live/engine.py`, `src/sca/live/reconcile.py`, `src/sca/live/persistence.py`, `src/sca/live/bybit_client.py` (✔C-P1#6 — read-only `normalize_order` +`clientOrderId`, NO order method), `src/sca/config.py`, `config/strategy.yaml`, `docs/decisions.md`, `docs/live-bybit-readonly-r1-plan.md`, `tests/test_reconcile.py`, `tests/test_config_runtime.py`, `tests/test_bybit_client.py` (shape assert only; `:133` no-order-method test untouched).
**Untouched (invariant):** the `BybitPrivateClient` **order surface** — it gains no order method; `test_client_exposes_no_order_methods` (`tests/test_bybit_client.py:133`) stays green.

### Invariant → test map
| Invariant | Test |
|---|---|
| PostOnly params correct, snapped price | `test_place_postonly_builds_correct_ccxt_call`, `test_place_passes_snapped_price_not_ccxt_round` |
| floor BUY / ceil SELL never cross | `test_quantize_buy_floors…` / `test_quantize_sell_ceils…` |
| qty floored to lot; lot param; qty_tol=lot/2 (F17) | `test_quantize_qty_floors_to_lot`, `test_desired_quantizes_qty_with_lot_param`, `test_qty_tol_defaults_half_lot` |
| PostOnly reject ≠ error (both forms) | `test_postonly_reject_returned_order…`, `test_postonly_reject_raised_exception…` |
| min-size dropped / classified, not retried (F19) | `test_desired_drops_below_min_qty_and_min_cost`, `test_min_size_invalid_order_classified_too_small_not_retried` |
| max_order_usd enforced BOTH layers (F11) | `test_desired_clamps_or_drops_above_max_order_usd`, `test_place_postonly_asserts_max_order_usd_raises` |
| dup link_id ALL retcodes idempotent (C-P2#16) | `test_dup_linkid_retcodes_all_idempotent` |
| order-state open-then-terminal by linkid (C-P0#1) | `test_fetch_state_open_by_linkid_then_terminal_by_linkid`, `test_fetch_state_never_calls_filled_only_closed_path`, `test_fetch_state_asserts_filled_finite`, `test_fetch_state_by_link_id_only_when_id_absent` |
| terminal covers cancelled+rejected (C-P0#2) | `test_fetch_terminal_state_covers_cancelled_and_rejected` |
| amend = total-qty / qty-only NO TIF / never on partial (F8, C-P1#7) | `test_amend_qty_only_no_tif_no_postonly`, `test_amend_refuses_price_change`, `test_amend_refuses_partially_filled_order`, `test_amend_total_qty_leaves_semantics`, `test_diff_compares_remaining_to_remaining` |
| 429 backoff | `test_rate_limit_backoff_retries_then_succeeds` |
| desired set formula | `test_desired_usd1…`, `test_desired_usdt…` |
| hysteresis: <1 tick ⇒ zero touch; ≥1bp re-prices (F15) | `test_within_one_tick_bucket_zero_touch`, `test_diff_unchanged_prices_all_leave_zero_touch`, `test_one_bp_anchor_move_reprices_affected_rungs`, `test_reconcile_zero_actions_when_anchor_submove` |
| aggregate avail bound (F16) | `test_desired_aggregate_avail_pool_decrements_bounded` |
| avail = free + own-locked, post-cancel validated (C-P1#13) | `test_avail_uses_free_plus_own_locked` |
| pure matcher: link_id→id→unambiguous-approx; ambiguity→unattributed (F5, C-P1#10, R2-P1) | `test_match_live_orders_returns_ambiguous_in_unattributed`, `test_fetch_open_exposes_client_order_id`, `test_get_open_orders_normalizes_keeps_client_order_id` |
| unattributable fill halts; cancel polls to terminal (R2-P0, R2-P1) | `test_cancel_polls_through_pending_cancel_until_terminal`, `test_unattributed_order_with_fill_halts_operator_reconcile` |
| Live.filled_qty drives amend-vs-recreate (C-P1#9) | `test_live_has_filled_qty`, `test_diff_qty_down_amends_unfilled_qty_up_or_partial_cancel_recreate` |
| full fill transitions; realized booked | `test_full_sell_fill…`, `test_full_buy_fill_books_realized_capture` |
| realized exact via persistent sell_proceeds (F6, C-P1#8) | `test_realized_uses_persistent_sell_proceeds`, `test_realized_capture_exact_under_multi_price_partial_sells` |
| _flip_state parity with paper, resets sell_proceeds/qty_sold (F18) | `test_flip_state_resets_same_fields_as_evaluate_fills`, `test_full_cycle_maker_realized_capture_parity_with_paper` |
| partial fill model | `test_partial_sell_updates_qty_cash_proceeds_keeps_state_usd1`, `test_partial_then_full_completes_transition` |
| cancel-first books residual; stale place aborted (C-P0#3, C-P0#4) | `test_cancel_books_fill_before_clear_cancel_first`, `test_stale_place_aborted_after_cancel_flips_state` |
| None/NaN guard both operands (F20) | `test_none_or_nonfinite_filled_total_guarded_skips` |
| anchor-None no-op (F19) | `test_reconcile_orders_noop_when_anchor_none`, `test_poll_fills_noop_when_anchor_none` |
| poll when EITHER id or link_id set (C-P1#11) | `test_poll_when_only_link_id_present` |
| min(calc, available) overshoot guard | `test_sell_qty_capped_at_available_no_overshoot` |
| readers value both legs; status split by leg (F2, C-P1#12) | `test_carry_sums_base_across_all_slices`, `test_status_doc_valuation_under_partial_fill`, `test_status_base_quote_value_independent_of_state`, `test_local_summary_sums_base_and_quote_across_all_slices`, `test_reconcile_proceeds_on_mid_partial_restart` |
| PostOnly-reject cooldown / halt (F9) | `test_postonly_reject_slice_cooldown_until_anchor_change`, `test_consecutive_postonly_rejects_trip_halt_threshold` |
| persist intent before place / fail-closed single point (F10) | `test_persist_intent_before_place`, `test_maker_fill_persist_oserror_halts_cancel_all`, `test_maker_fill_single_persist_point` |
| v2 + v1 migration synced (incl. sell_proceeds/qty_sold) | `test_order_fields_roundtrip_v2`, `test_v1_state_migrates…`, `test_resume_typecheck_rejects…` |
| restart reconciliation; lost-fill + uncertain-retry (F14, F23, C-P0#5) | `test_restart_matches…`, `…cancels_orphan…`, `…refuses_foreign…`, `test_crash_after_place_before_id_persist_recovers_fill_while_down`, `test_uncertain_retry_fetches_state_never_two_live_orders`, `test_resume_uses_passed_open_orders_no_refetch`, `test_resume_uses_gate_fetched_open_orders` |
| maker-aware R1 / taker preserved / ownership (F1, F23) | `test_reconcile_expected_maker_orders_proceeds`, `…orphan_refuses`, `…empty_expected_preserves_taker_refuse`, `test_reconcile_balance_still_checked_with_orders`, `test_reconcile_decides_resume_applies_ownership_split` |
| single testnet resolver, no split-brain (F13) | `test_resolve_testnet_env_over_runtime_over_default`, `test_live_testnet_redirects_to_runtime_testnet`, `test_both_clients_get_identical_testnet_no_split_brain` |
| maker_enabled rollback knob (C-P1#14) | `test_resolve_maker_enabled_precedence`, `test_maker_enabled_off_falls_back_to_paper_path` |
| run-loop wiring: skip paper paths, cadence, accrue-before-poll (F4) | `test_evaluate_fills_and_maybe_deploy_skipped_when_maker_enabled`, `test_maker_step_invoked_on_armed_path`, `test_carry_accrues_before_poll_fills_across_hour` |
| seed-in-gate before decide; refuse mixed; reaches lifecycle (F3, C-P0#5, C-P1#15) | `test_seed_before_reconcile_decides`, `test_seed_refuses_mixed_balance`, `test_armed_maker_testnet_start_emits_place_action` |
| _r1_ok defense-in-depth; fresh_deploy stays refused (F22) | `test_reconcile_orders_asserts_r1_ok`, `test_poll_fills_asserts_r1_ok`, `test_fresh_deploy_still_refused_on_testnet_and_mainnet` |
| kill switch / cancel-all-on-exit + signal (F12) | `test_run_exit_cancels_all_resting_orders`, `test_sigterm_triggers_cancel_all` |
| gates (testnet-only, logical flags, ctor refuse mainnet F21) | `test_place_postonly_refused_on_mainnet`, `test_maker_client_refuses_construction_on_mainnet`, `test_armed_maker_only_when_live_confirm_keys_testnet_maker` |
| read-client no order method (invariant) | `test_client_exposes_no_order_methods` (unchanged, `:133`) |
| paper untouched | `test_paper_mode_never_builds_order_client_still_simulates` + existing 130 |

### Rollback plan
Feature gated by `maker_enabled = armed and resolve_testnet() and resolve_maker_enabled()` (three logical flags, config-driven). Turn **`resolve_maker_enabled` off** (env or `runtime.maker_enabled: false`, ✔C-P1#14) → engine falls back to `evaluate_fills` (paper path), zero behavior change — no code revert needed. Worktree branch is unmerged; `git revert`/branch-delete removes everything; main is unaffected until the owner authorizes merge. No DB/schema migration on main (v2-schema ships with v1-migration, reversible: a v2 state with empty order fields + zero sell_proceeds/qty_sold reads identically to v1 semantics). `bybit_client.normalize_order`'s extra `clientOrderId` key is additive/read-only. Doc changes (D11 + R1 note) are additive prose.

### Definition of done = merge-ready (NOT merged, NOT live)
1. All new RED→GREEN tests pass + existing 130 stay green (`PYTHONPATH=src python3 -m pytest tests/ -q`).
2. `orders.py`, `order_recon.py`, and new engine maker paths at ~100% line+branch (Lee red line: a wrong order = P0).
3. ce multi-persona review + qa quality terminal review + **one** Codex (`gpt-5.5`, effort high, read-only) heterogeneous audit clean (no P0/P1). **F6/C-P1#8's realized-capture reduction-to-paper is confirmed** with the persistent `sell_proceeds`/`qty_sold` fields (A4) — the v2 open note is closed, not left ambiguous.
4. Order-state retrieval is grounded (open-by-linkid then terminal-by-linkid; cancels/rejects observable); cancel never drops a fill (cancel-first rebooking) and never runs a stale place across a state change; amend is qty-only with no TIF; all dup-linkid retcodes idempotent.
5. Mainnet placement provably refused at BOTH ctor and place (testnet-only); `fresh_deploy` still refused on both venues; R1 gate maker-aware; single testnet resolver + maker_enabled knob (no split-brain); seeding runs before reconcile decides and refuses a mixed balance; kill-switch cancels all resting orders on any exit; paper provably never builds the order client; read-client gains no order method.
6. Manual testnet smoke documented as the **3b entry gate**, not a merge blocker.
7. D11 recorded + R1 plan note added (governance debt cleared).
8. NOT merged to main (hard rule #4 — owner's call), NOT live on mainnet, NO economics claims (that is 3b).

### Resolved open decisions (my call + why — for heterogeneous review)
- **D0 MAKER (DECIDED, not re-asked) (✔F1):** owner re-locked TAKER→MAKER; recorded as D11 + an R1-plan note rather than flagging it open.
- **Order-state via open-then-terminal by link_id (✔C-P0#1/#2):** `fetch_open_orders(orderLinkId)` then `fetch_canceled_and_closed_orders(orderLinkId)`; never the Filled-only `fetch_closed_order`. This is the only mechanism that observes a Cancelled/Rejected order or cancelled-partial — the crux Codex blocked v2 on.
- **Cancel-first rebooking + stale-place abort (✔C-P0#3/#4):** cancel, then re-poll terminal, book the final `cumExecQty`, then clear; if that booked a delta/flipped state, drop the precomputed place and re-derive next tick. No fill is dropped to a cancel race; no stale wrong-side order is sent across a transition.
- **Seed INSIDE the gate, before reconcile decides; refuse mixed (✔C-P0#5/✔C-P1#15):** armed-maker testnet seeds slices from the reconciled balance *before* `reconcile()` compares, so a USDT-funded account proceeds (not fresh_deploy-refused); a mixed/ambiguous balance or pre-existing orders refuses. Gate stores its report + open-orders for resume to reuse (no refetch). `fresh_deploy` stays UNCONDITIONALLY refused; mainnet unreachable.
- **State-switched readers value BOTH legs; status valuation split by leg (✔F2/✔C-P1#12):** the binary state machine leaves a transient mixed state during a partial; `_usd1_qty`/`_slice_value`/`_local_summary` generalize to all slices, and `status_doc` computes `base_value=Σqty·mark` / `quote_value=Σcash` independent of state (strict superset; equals v1 on clean states).
- **Realized via persistent sell_proceeds/qty_sold (✔F6/✔C-P1#8):** blended `avg_sell` is exact under multi-price partial sells and reduces to paper's `(R−B)·nq` on a single-price full cycle — backed by real persisted fields, closing the v2 open note.
- **Pure `match_live_orders` (✔C-P1#10) + `Live.filled_qty` (✔C-P1#9):** matching/ambiguity has one pure, tested home in `order_recon.py`; link_id authoritative; same-price ambiguity REFUSES, never guesses.
- **amend = qty-only, NO TIF/postOnly, total-qty semantics, never on a partial (✔F8/✔C-P1#7):** ccxt amend schema accepts neither postOnly nor timeInForce and the exchange preserves them; price/qty-up/partial → cancel+recreate (booking residual first, ✔F7/✔C-P0#3).
- **avail = free + own-locked, new placement validated post-cancel (✔C-P1#13):** neither raw free (force-cancels valid orders) nor wallet (overcommits).
- **maker_enabled rollback knob (✔C-P1#14):** `env > runtime.maker_enabled > default false`; off ⇒ paper path, zero-code rollback.
- **poll when EITHER id or link_id (✔C-P1#11):** consistent with crash-after-place recovery (F14).
- **read-client keeps clientOrderId, no order method (✔C-P1#6):** one read-only field so resume matches by link_id; `:133` invariant intact.
- **Hysteresis 1 full tick (✔F15) + aggregate avail bound (✔F16); fail-closed + single persistence point + kill-switch (✔F10, ✔F12); single testnet resolver (✔F13); restart owns side-effects, reconcile owns the decision (✔F23/✔C-P0#5):** unchanged from v2 except where a Codex fix above refines them.

**Plan file (in the worktree, not main):** `/workspace/stablecoin_arbi/.claude/worktrees/phase3a-maker-orders/docs/phase3a-maker-orders-plan.md`
