# Plan — Phase 1+2: Bybit private read-only client + R1 reconciliation

**Branch:** `worktree-live-bybit-readonly-r1`
**Scope (boss-locked):** Phase 1 (UTA wallet-balance read) + Phase 2 (R1 startup reconciliation). **Read-only — ZERO order placement.**
**SDK (boss-locked):** ccxt 4.5.54.
**Execution model (boss-locked, applies in Phase 3):** market data via WebSocket (already the case); trading via **taker** (0-fee) → realized as IOC marketable-limit.
> **SUPERSEDED (2026-06-19, see `docs/decisions.md` D11):** the Phase-3 execution model has been re-locked to **MAKER** (PostOnly resting ladder). This "trading via taker … applies in Phase 3" line no longer holds; Phase 3a builds the maker primitive and makes the R1 reconcile maker-aware.

---

## 1. Goal / Non-goals

**Goals**
- **G1** — `BybitPrivateClient` (ccxt): signed read of the **UTA** wallet balance. API keys from **env only**.
- **G2** — `sca balance` CLI: print USD1/USDT `wallet/free/locked` + account USD totals.
- **G3** — **R1 reconciliation**: on **armed-live** startup, query real balance + open orders, compare to local `<symbol>_state.json`, and **gate** the currently fail-open "corrupt/missing-state → fresh-deploy" path behind a passing reconciliation.

**Non-goals (explicitly OUT of this scope)**
- **N1** — Real order placement / wiring `OrderInterface` → **Phase 3, requires separate boss authorization** (hard rule #3).
- **N2** — Changing the paper/backtest fill model to taker economics → Phase 3 + a backtest-fidelity task.
- **N3** — Private WS (order/execution/wallet push) → Phase 3.
- **N4** — Sizing real orders from real balance (alloc is currently config `$10k`) → Phase 3 open question (§9 R-d).

---

## 2. Context (grounded in current code)

- All Bybit I/O today is **public / no-key**: `data/fetch.py` (REST kline), `tools/dryrun.py` + `live/engine.py` (public WS orderbook/trades/klines).
- Safety gate already exists:
  - `live/engine.py:135` `live_authorization(mode)` → armed only when `mode=="live"` **and** `LIVE_TRADING_CONFIRM=="yes"` **and** `BYBIT_API_KEY`/`BYBIT_API_SECRET` present.
  - `live/engine.py:148` `OrderInterface.place_order()` raises (intentional Phase-3 scaffold).
  - `config/strategy.yaml:74-85` `live:` block — env var names, `max_order_usd: 2000`, `persist`.
- **R1 is already documented as a LIVE BLOCKER**: `docs/decisions.md` D10/R1 + `docs/engine-persist-resume-plan.md:128` — local state is necessary but **NOT sufficient** for real orders; before real placement, startup **MUST reconcile vs the exchange**; the corrupt/missing-state → fresh-deploy path is **fail-OPEN** and must be gated behind reconciliation. Today it is safe only because fills are simulated.
- **ccxt 4.5.54** (installed, introspected): bybit auto-detects UTA (`is_unified_enabled()` → sets `accountType=UNIFIED`), `fetchOpenOrders` present, spot taker market orders handled (`createMarketBuyOrderRequiresPrice=False` on UTA + `marketUnit`). **Gotcha: `defaultType` defaults to `swap` → must force `spot`.**

---

## 3. Design decisions

- **D-a — ccxt for private REST; keep raw WS for market data.** Existing zero-dep WS feed is untouched; ccxt is used ONLY for private REST (balance, open orders, later orders). ccxt becomes a **declared** dependency (currently only incidentally installed).
- **D-b — Read-only first.** Phase 1+2 place no orders. Recommend the boss provision a **read-only API key** (no trade permission) for these phases → defense-in-depth; a trade-enabled key is introduced only at Phase 3.
- **D-c — Taker recorded (Phase 3).** 0-fee removes the *fee*, not the *spread*. The current backtest/paper books fills at the rung R/B while a taker fills at `bid≥R` / `ask≤B` — but (Codex P1) **"conservative-on-price" only holds IF an IOC marketable-limit fully fills within the rung limit.** The current model assumes a *full slice fill on touch*; live taker can **underperform** via partial fills, missed fills after latency, insufficient depth at the limit, IOC residual cancels, and the resulting local/exchange state divergence. So: use **IOC marketable-limit** (slippage cap on a thin book), and Phase 3 MUST add **depth-aware sizing, partial-fill state handling, missed-fill accounting, and a backtest mode that models depth/liquidity/IOC residuals** before live PnL is trusted. USD1 carry stays the dominant return regardless.
- **D-d — Paper path stays byte-identical.** The reconcile gate activates ONLY when `self.armed`. Paper never calls the private API. This is regression-tested.
- **D-e — Testnet toggle.** `live.testnet` routes ccxt sandbox mode; validates the signing/plumbing risk-free. Caveat: USD1USDT may be absent on Bybit testnet — testnet proves signing, not USD1 specifically.

---

## 4. Architecture / file map

```
src/sca/live/
  creds.py          (NEW)  single credential resolver (config var-names → key/secret/confirm)
  bybit_client.py   (NEW)  BybitPrivateClient — ccxt wrapper: spot+UTA, env keys, testnet, verbose=False, redacted repr
  reconcile.py      (NEW)  reconcile() + ReconcileReport — PURE compare (data in, no I/O)
  engine.py         (EDIT) (1) live_authorization() → use creds resolver (drop hardcoded env names);
                           (2) _reconcile_or_refuse() gate in run(); armed-only; before bootstrap()
src/sca/cli.py      (EDIT) `sca balance` subcommand; `sca live --allow-fresh-live-deploy` first-start flag
config/strategy.yaml(EDIT) live.testnet, live.account_type, live.dedicated_account (default true)
pyproject.toml      (EDIT) dependencies += ccxt
requirements.txt    (EDIT) ccxt
tests/              (NEW)  test_creds.py, test_bybit_client.py, test_reconcile.py, test_engine_recon_gate.py
```

---

## 5. Data structures

**Normalized balance** (decoupled from ccxt's shape so consumers don't bind to ccxt internals):
```python
{
  "totals": {"equity_usd": float, "available_usd": float, "wallet_usd": float},
  "coins": {
    "USD1": {"wallet": float, "free": float, "locked": float, "usd": float},
    "USDT": {...},
  },
  "raw": {...},   # ccxt 'info' passthrough, for audit/debug only
}
```
**ReconcileReport**:
```python
{
  "ok": bool,                       # exchange consistent with local within tolerance
  "action": str,                    # "proceed" | "fresh_deploy" | "refuse"
  "exchange": {coin: {"wallet": float, "free": float, "locked": float}},
  "exchange_clean_start": bool,     # no open orders + capital all in funding coin
  "open_orders": [{"id","side","price","qty","symbol"}],
  "local": {"usd1_qty": float, "usdt_value": float, "deployed": bool, "resumed": bool},
  "discrepancies": [str],           # human-readable mismatch lines (empty when ok)
}
```
The engine gate (P2.2) maps `action` → proceed / fresh-deploy / loud-refuse.

---

## 6. Task breakdown (strict TDD: RED → GREEN → REFACTOR)

### Phase 1 — read-only balance
- **P1.0** `live.creds` — single credential resolver (Codex P1, env-name drift): one helper reads the env-var **names** from config (`live.confirm_env`/`api_key_env`/`api_secret_env`) and returns the resolved key/secret/confirm. **`live_authorization()` (`engine.py:139-144`) currently HARDCODES `BYBIT_API_KEY`/`BYBIT_API_SECRET`/`LIVE_TRADING_CONFIRM`** — refactor it to use this resolver so the arm-check and `BybitPrivateClient` can never diverge.
  - RED: custom env-var names in config → both `live_authorization()` and the client resolve the same key; default names still work (back-compat).
- **P1.1** `BybitPrivateClient.__init__(testnet=None)` — keys via the P1.0 resolver; build `ccxt.bybit({'apiKey','secret','enableRateLimit':True,'verbose':False,'options':{'defaultType':'spot'}})`; `testnet → set_sandbox_mode(True)`. Missing keys → clear `RuntimeError`. Keys never appear in `repr`/logs; `verbose` pinned `False`.
  - RED: missing keys raises; testnet→sandbox; `defaultType=='spot'`; `verbose is False`; `repr` redacts secret.
- **P1.2** `get_wallet_balance()` — `fetch_balance({'type':'unified'})` → normalize to §5. **Normalize from the raw V5 fields in `balance['info']` (`result.list[0].coin[]`: `walletBalance`, `locked`, `usdValue`, `equity`) — verified against official docs — and use ccxt's `free/used/total` only as a cross-check.** Rationale (review C1): for a UTA, ccxt's `used` reflects *margin used*, not the spot open-order `locked`; trusting `used==locked` could misreport free balance. `free := walletBalance - locked`.
  - RED: canned ccxt response (with realistic `info`) → asserts USD1/USDT `wallet`/`locked`/`free`/`usd` + account totals; assert raw-vs-ccxt cross-check path.
  - ⚠️ (Codex P1) `free := walletBalance - locked` is **display-grade only**, NOT a general UTA spendable invariant — UTA has borrow/`spotBorrow`, margin collateral, account IM/MM, and possible negative equity. The normalized doc therefore also carries `totals.equity_usd`, account `borrow`/`liability`, and `marginUsed` (when present in `info`) so the **reconcile** safety guards (P2.1) can refuse on them. Display (`sca balance`) shows `wallet-locked` but flags any non-zero borrow/liability.
- **P1.3** `get_open_orders(symbol)` — passthrough `fetch_open_orders(symbol)` → normalized list (used by P2).
  - RED: canned → normalized order dicts.
- **P1.4** `sca balance [--testnet]` — construct client, print table (USD1/USDT rows + totals). Read-only.
  - RED: invoke with monkeypatched client → output contains USD1 row + total equity.
- **P1.5** config + deps — `live.testnet: false`, `live.account_type: unified`, `live.dedicated_account: true` (R-g); add `ccxt` to pyproject + requirements.

### Phase 2 — R1 reconciliation
- **P2.1** `reconcile(local_state, exchange_balance, open_orders, *, tol, dedicated)` — **PURE compare fn, no I/O** (review A1): all exchange data is passed in already-fetched, so it unit-tests with plain dicts. Computes local implied holdings from `local_state` slices (Σ `qty` in `usd1` coins, Σ `cash` in `usdt`), compares to exchange **coin quantities** (apples-to-apples; notional alloc is irrelevant — review R-d). Emits `ReconcileReport`.
  - On a **dedicated** account: exchange USD1/USDT must equal local within `tol`.
  - On a **shared UTA** (`dedicated=False`): strategy holdings are a *subset* → check `exchange ≥ local - tol` (lower bound) and flag that exact match isn't asserted (review A3 / R-g).
  - RED: matching → `ok=True`; qty diff > tol → `ok=False` + discrepancy; shared-UTA lower-bound path; open orders surfaced.
- **P2.2** `engine._reconcile_or_refuse()` — called in `run()` when `self.armed`, **before** `bootstrap()`. Keys off EXCHANGE truth, with an **explicit first-start protocol** (Codex P0). Preconditions checked first:
  - **Persist must be on** (Codex P1): armed + `live.persist == False` → **refuse** (with persist off, every restart looks like a first start → R1 cannot guarantee restart safety). Armed live requires persistence.
  - **Liability/margin guard** (Codex P1): fetch balance; if account shows non-zero `borrow`/`liability`/`marginUsed`, negative equity, or `equity_usd` materially ≠ `wallet_usd`, or account is not spot-only-unified → **refuse** (`walletBalance - locked` is not spendable truth under margin/borrow).
  - **Account-wide open orders** (Codex P2): fetch open orders **account-wide** (not just `symbol`); any open order or account-level lock indicating off-strategy activity → refuse (dedicated-account expectation).

  Then the decision:
  - **Local state present** (`self._resumed`): `reconcile(...)`; `ok=False` → **refuse** (loud).
  - **Local state absent/corrupt** (`not self._resumed`): balances **cannot** distinguish a legitimate pre-bought-USD1 first start from a lost-state live position whose slices are all USD1. So fresh deploy is **NEVER inferred from balances** — it requires an **explicit one-time operator opt-in**: `sca live --allow-fresh-live-deploy` (or a sentinel file), which also declares the expected starting asset + amount. The gate then verifies the exchange matches that declaration (expected asset, amount within `tol`, no other holdings/orders) → fresh deploy; mismatch or **no opt-in** → **refuse** with a message telling the operator to either pass the first-start flag or seed local state manually.
  - **Refuse = loud + non-zero exit** (review S1): clear stderr + `SystemExit(non-zero)`; never a silent hang or silent downgrade-to-paper.
  - `not self.armed` (paper) → **no-op**; the private client is **never even constructed** (Codex confirmed the `__init__`/`_maybe_resume`/`bootstrap`/unarmed-`run` path needs no private client).
  - RED: (a) armed + no state + `--allow-fresh-live-deploy` + exchange matches declaration → deploys; (b) armed + no state + **no** opt-in → refuses non-zero, no deploy (even if all-USD1); (c) armed + no state + opt-in but exchange mismatch → refuses; (d) armed + resumed matching → proceeds; (e) armed + resumed mismatch → refuses; (f) armed + `persist=false` → refuses; (g) armed + non-zero borrow/margin → refuses; (h) **paper → `BybitPrivateClient` never instantiated** (monkeypatch its `__init__` to raise; assert not raised).
- **P2.3** wire into `run()` preserving paper resume/bootstrap order; armed-only branch. ccxt sync calls block the asyncio loop — acceptable here (startup, before the WS loop), documented (review C2).

Self-review after all points (logic correctness, races, var shadowing, backward-compat) per CLAUDE.md TDD规范.

---

## 7. Safety / security

- API keys: **env only** (config supplies var names), never logged / committed / included in `repr`. Verify `.env`/key files are gitignored. **Keep ccxt `verbose=False`** (review S2) — verbose mode logs signed request headers (key + signature).
- **Boss decision: a trade-capable key is used** (not read-only). This removes the exchange-level "key physically can't place orders" guarantee → **compensate in code (HARD requirement):** `BybitPrivateClient` exposes **no** order-placing method in Phase 1+2, and tests **spy the ccxt mock and assert `create_order`/`create_limit_order`/`create_market_order`/`cancel_order`/`cancel_all_orders` are NEVER invoked** on any Phase 1+2 code path. "No orders" becomes a *tested invariant*, not just an absence. Recommend the boss still scope the key to spot + no-withdraw.
- **Boss decision: dedicated subaccount** → `live.dedicated_account: true`; reconcile asserts exact equality (not the degraded lower-bound).
- Reconcile gate runs **only when armed-live**; paper path proven byte-identical (P2.2).
- **Armed live requires `persist=true`** (Codex P1) — refuse otherwise.
- **Fresh deploy under armed live is never inferred from balances** — requires explicit `--allow-fresh-live-deploy` + matching exchange declaration (Codex P0).
- **Dedicated (sub)account is the default for armed live** (`live.dedicated_account: true`); shared UTA requires an explicit risk-accept override and the gate reports **DEGRADED**, not `ok` (Codex P1).
- **Reconcile refuses on UTA liability** — non-zero borrow/margin, negative equity, or off-strategy open orders (Codex P1/P2).
- No order path touched: `OrderInterface.place_order()` still raises (Phase 3). `max_order_usd` unchanged.

---

## 8. Validation

- **Unit**: mocked ccxt (zero network) for every client/reconcile/gate test.
- **Integration (manual, boss-run)**: `sca balance --testnet` with a testnet key → signing/plumbing proof; then a **read-only mainnet key** → real USD1/USDT balance (testnet may lack USD1USDT).
- **Regression**: full existing suite green; a paper engine run produces byte-identical status vs `main` (no behavioral drift).
- **Paper-canary (feedback 铁律)**: before ANY Phase 3, run the armed-live engine real-start ≥10 min on the server (reconcile + still-simulated fills) to surface integration bugs (rate-limit/reconnect/clock) that unit tests can't.

---

## 9. Risks / open questions

- **R-a** ccxt UTA balance routing — grounded by introspection; verify live on testnet + a mainnet read-only key.
- **R-b** USD1USDT may not exist on Bybit testnet → testnet validates signing, not USD1 specifically.
- **R-c** Clock skew / `recv_window` → ccxt manages; expose `options['recvWindow']` if drift appears.
- **R-d** **Alloc source**: live real capital ≠ config `$10k`. Reconcile reports both; sizing-from-real-balance is **Phase 3** (flagged here, not solved).
- **R-e** Reconcile tolerance: dust/rounding → `tol` param (default conservative); refuse on real mismatch, don't silently pass.
- **R-f** The `engine.run()` gate is the **only non-additive edit** → covered by the P2.2 "paper path unchanged" regression test.
- **R-g (BOSS DECISION) Account topology**: reconciliation is exact only on a **dedicated** (sub)account. On a **shared UTA**, unrelated USD1/USDT can **mask** a real deficit in the bot's position (an external trade / missed fill / wrong local slice can still pass `exchange ≥ local`) — Codex P1. So **dedicated subaccount is MANDATORY for armed live by default** (`live.dedicated_account: true`); a shared UTA requires an explicit risk-accept override, and the gate then reports **DEGRADED** (not `ok`). `reconcile(dedicated=...)` covers both. → *Need your call: dedicated Bybit subaccount for live? (recommended)*
- **R-h** ccxt `is_unified_enabled()` makes an extra account probe on first balance fetch (read endpoint — works with a read-only key). Mocked in tests; one extra round-trip at startup live.
- **R-i (Codex P1)** Armed live with `persist=false` makes every restart look like a first start → R1 void. Mitigation: refuse armed live unless `persist=true`.
- **R-j (Codex P1)** Credential env-name drift between the hardcoded `live_authorization()` and the config-driven client → unified via the P1.0 `creds` resolver (single source of truth).

---

## 10. Rollback

Everything is additive except the `engine.run()` armed-only gate branch. Rollback = revert the worktree commits; config additions are backward-compatible (`testnet` defaults `false`, `account_type` is informational). Paper and backtest behavior are unaffected throughout.

---

## 11. Review trail (plan审查 — both gates passed)

**第一道 persona self-review** → 6 findings folded in: A2(P0) gate keys off exchange-truth; A1(P1) pure `reconcile`; A3(P1) shared-UTA lower-bound; C1(P1) normalize from raw V5 `info`; S1(P1) loud non-zero refuse; S2(P2) ccxt `verbose=False`.

**第二道 Codex 异构审查** (gpt-5.5, high, read-only) → 8 findings, all accepted (verified against code, no hallucinations):
- **P0** "clean-start from balances" can't distinguish pre-bought-USD1 first start from lost-state-over-USD1-position → **explicit `--allow-fresh-live-deploy` first-start protocol** (P2.2).
- **P1** `persist=false` defeats R1 → refuse armed live without persistence (P2.2 / R-i).
- **P1** first-start matrix incomplete → folded into the explicit-opt-in protocol.
- **P1** `free=walletBalance-locked` unsafe under UTA borrow/margin → reconcile **liability guard** + display-grade caveat (P1.2 / P2.2).
- **P1** shared-UTA lower-bound masks drift → **dedicated account mandatory by default**, shared = DEGRADED (R-g).
- **P1** credential env-name drift (`live_authorization` hardcodes names) → **single `creds` resolver** (P1.0).
- **P2** open-orders scoped to symbol misses off-symbol locks → **account-wide** open-order check (P2.2).
- **P1** taker "conservative-on-price" too broad (ignores partial/missed fills, depth, IOC residuals) → **reworded** + Phase-3 depth/partial-fill requirements (D-c).

Codex verdict: *"not safe to implement as-is"* under the original plan → the above changes close those gaps. Paper-parity confirmed safe.

**No Codex round-2 on the plan** (per CLAUDE.md: fixes don't change cross-task interfaces; 信任修复的测试覆盖).

**第三道 Codex 异构审查 — FINAL CODE (merge gate)** (gpt-5.5, high, read-only) → 5 more findings, all accepted & fixed:
- **P0** `reconcile.py` clean-start still let all-USD1 + opt-in pass as fresh deploy → fresh deploy now requires an explicit `--expect-asset`/`--expect-amount` **declaration** matching the exchange (balances never infer intent).
- **P1** resumed branch ignored open orders → **any open order refuses on every path** (taker bot leaves none; an open order is an anomaly).
- **P1** `_liability_reason` missed UTA margin fields → `normalize_balance` now captures `totalInitialMargin`/`totalMaintenanceMargin`/`totalPerpUPL` and the guard refuses on any non-zero.
- **P2 ×2** the "all-USD1" test didn't test all-USD1; no resumed+open-order test → both added.
- Codex verified: `SystemExit(3)` is raised before the reconnect `except Exception` loop (not swallowed); `_maybe_gate` is armed-only. Real-path note: `fetch_open_orders(None)` account-wide coverage for bybit spot UTA to be confirmed on testnet (logic is tested; the exact ccxt params are a real-path item).

Final: **117 tests green** (80 original, zero drift + 37 new); engine imports without ccxt (paper safety).

## 12. Open decisions for the boss (审批)

1. **Dedicated Bybit subaccount for live?** (recommended — makes reconcile exact; shared UTA only gives a degraded lower-bound). [R-g]
2. **Read-only API key for Phase 1+2?** (recommended — can't place orders even on a bug). [D-b]
3. First-start UX: `--allow-fresh-live-deploy` flag vs sentinel file — either works; flag is simpler.
