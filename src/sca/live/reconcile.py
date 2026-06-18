"""R1 reconciliation — the PURE decision brain (no I/O).

Given the local engine state and already-fetched exchange truth (balance + open
orders), recommend an action: ``proceed`` | ``fresh_deploy`` | ``refuse``.

R1 invariant (Codex P0): with NO local state, a fresh deploy over real money is
NEVER inferred from balances alone — balances cannot tell a legitimate
pre-funded first start from a lost-state live position. Fresh deploy therefore
requires an explicit operator opt-in (``allow_fresh``) AND a clean exchange (one
side dust, no open orders). A resumed state must match the exchange: exact within
``tol`` for a dedicated account; a lower-bound (and DEGRADED) check for a shared
UTA, where unrelated capital can mask drift (Codex P1).

The engine (``_reconcile_or_refuse``) does the I/O + the persist/liability
preconditions, then calls this and maps the action.
"""
from __future__ import annotations


def _wallet(exchange: dict, coin: str) -> float:
    return exchange.get("coins", {}).get(coin, {}).get("wallet", 0.0)


def reconcile(local, exchange, open_orders, *, base_coin, quote_coin,
              tol: float = 1.0, dedicated: bool = True, allow_fresh: bool = False,
              expect_asset: str | None = None, expect_amount: float | None = None,
              expected=None) -> dict:
    """Return a ReconcileReport. ``local`` is None (no/lost state) or a summary
    ``{'resumed':bool,'deployed':bool,'base_qty':float,'quote_qty':float}``.

    A fresh deploy (no local state) requires BOTH the operator opt-in
    (``allow_fresh``) AND an explicit declaration (``expect_asset``/``expect_amount``)
    that matches the exchange — balances alone are never trusted to infer intent
    (Codex P0).

    ``expected`` (F1, maker-aware) is the set of OUR resting-order ``link_id``s
    (``clientOrderId``). A maker strategy leaves resting orders BY DESIGN, so an
    open order whose ``clientOrderId`` is in ``expected`` is NOT an anomaly. Any
    UNEXPECTED open order (foreign, or a stale/orphan link we no longer expect) is
    still off-strategy activity and refuses on EVERY path. ``expected`` empty/None
    => every order is unexpected => the OLD taker refuse-on-any-order behavior is
    preserved exactly (the 13 legacy tests). This function only DECIDES (F23): it
    performs NO I/O and NO side-effects — the engine's ``resume_reconcile_orders``
    applies the ownership split (re-link / cancel-orphan / refuse-foreign) on the
    same already-fetched ``open_orders`` list."""
    ex_base = _wallet(exchange, base_coin)
    ex_quote = _wallet(exchange, quote_coin)
    expected_links = set(expected) if expected else set()
    unexpected = [o for o in open_orders
                  if o.get("clientOrderId") not in expected_links]
    has_unexpected = bool(unexpected)
    has_orders = bool(open_orders)
    clean = (not has_orders) and (ex_base <= tol or ex_quote <= tol)

    disc: list[str] = []
    report = {
        "exchange": {base_coin: exchange.get("coins", {}).get(base_coin, {}),
                     quote_coin: exchange.get("coins", {}).get(quote_coin, {})},
        "exchange_clean_start": clean,
        "open_orders": open_orders,
        "local": local,
        "discrepancies": disc,
    }

    def _refuse(msg: str) -> dict:
        disc.append(msg)
        report["ok"] = False
        report["action"] = "refuse"
        return report

    # GLOBAL precondition (Codex P1, now maker-aware F1): refuse on EVERY path if any
    # UNEXPECTED open order is present, before trusting balances. EXPECTED maker resting
    # orders (link_id in `expected`) are by-design and do NOT trip this. Empty `expected`
    # => every order is unexpected => the old taker refuse-on-any-order is preserved.
    if has_unexpected:
        return _refuse(f"{len(unexpected)} unexpected open order(s) on the account — "
                       "refusing (expected maker links are by-design; the rest are "
                       "off-strategy; investigate before trading)")

    if local and local.get("resumed"):
        lb = float(local.get("base_qty", 0.0))
        lq = float(local.get("quote_qty", 0.0))
        if dedicated:
            ok_base = abs(ex_base - lb) <= tol
            ok_quote = abs(ex_quote - lq) <= tol
        else:
            ok_base = ex_base >= lb - tol
            ok_quote = ex_quote >= lq - tol
            disc.append("shared-UTA: lower-bound reconcile only (DEGRADED) — "
                        "unrelated capital can mask a real deficit")
        if not ok_base:
            disc.append(f"{base_coin}: exchange {ex_base} vs local {lb} (tol {tol})")
        if not ok_quote:
            disc.append(f"{quote_coin}: exchange {ex_quote} vs local {lq} (tol {tol})")
        ok = ok_base and ok_quote
        report["ok"] = ok
        report["action"] = "proceed" if ok else "refuse"
        return report

    # No local state (first start, or lost/corrupt).
    if not allow_fresh:
        return _refuse("no local state and fresh live deploy not authorized — pass "
                       "--allow-fresh-live-deploy WITH --expect-asset/--expect-amount for a "
                       "genuine first start, or seed local state")
    if not expect_asset or expect_amount is None:
        return _refuse("fresh deploy requires an explicit declaration: pass "
                       "--expect-asset <COIN> --expect-amount <N> (balances cannot be trusted "
                       "to infer a first start vs a lost-state position)")

    holdings = {base_coin: ex_base, quote_coin: ex_quote}
    if expect_asset not in holdings:
        return _refuse(f"declared asset {expect_asset!r} is not the traded pair's "
                       f"base/quote ({base_coin}/{quote_coin})")
    declared = holdings[expect_asset]
    other = ex_quote if expect_asset == base_coin else ex_base
    if abs(declared - float(expect_amount)) > tol:
        return _refuse(f"declared {expect_asset}={expect_amount} but exchange holds "
                       f"{declared} (tol {tol})")
    if other > tol:
        other_coin = quote_coin if expect_asset == base_coin else base_coin
        return _refuse(f"non-declared coin {other_coin}={other} is not dust — looks like an "
                       "existing position, not a clean first start; seed local state instead")

    report["ok"] = True
    report["action"] = "fresh_deploy"
    return report
