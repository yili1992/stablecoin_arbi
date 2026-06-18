"""Tests for the pure reconcile() fn (sca.live.reconcile) — Phase 2 / R1, T4.

reconcile() takes already-fetched data (no I/O) and recommends an action:
  proceed | fresh_deploy | refuse.

This is the R1 brain (Codex P0): with NO local state, a fresh deploy is NEVER
inferred from balances — it requires an explicit operator opt-in (allow_fresh) AND
a clean exchange. A resumed state must match the exchange (exact for a dedicated
account; lower-bound + DEGRADED for a shared UTA).

Run: PYTHONPATH=src python3 -m pytest tests/test_reconcile.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.reconcile import reconcile  # noqa: E402

BASE, QUOTE = "USD1", "USDT"


def _bal(usd1=0.0, usdt=0.0, **extra):
    coins = {
        "USD1": {"wallet": usd1, "locked": 0.0, "free": usd1, "usd": usd1, "borrow": 0.0},
        "USDT": {"wallet": usdt, "locked": 0.0, "free": usdt, "usd": usdt, "borrow": 0.0},
    }
    return {"account_type": "UNIFIED", "coins": coins, "totals": {}, **extra}


# --- resumed state: must match exchange ------------------------------------

def test_resumed_dedicated_match_proceeds():
    local = {"resumed": True, "deployed": True, "base_qty": 6000.0, "quote_qty": 4000.0}
    r = reconcile(local, _bal(usd1=6000.0, usdt=4000.0), [],
                  base_coin=BASE, quote_coin=QUOTE, tol=1.0, dedicated=True)
    assert r["ok"] is True and r["action"] == "proceed"
    assert r["discrepancies"] == []


def test_resumed_dedicated_mismatch_refuses():
    local = {"resumed": True, "deployed": True, "base_qty": 6000.0, "quote_qty": 4000.0}
    r = reconcile(local, _bal(usd1=5000.0, usdt=4000.0), [],   # 1000 USD1 short
                  base_coin=BASE, quote_coin=QUOTE, tol=1.0, dedicated=True)
    assert r["ok"] is False and r["action"] == "refuse"
    assert any("USD1" in d for d in r["discrepancies"])


def test_resumed_shared_uta_lowerbound_ok_but_degraded():
    local = {"resumed": True, "deployed": True, "base_qty": 6000.0, "quote_qty": 4000.0}
    # exchange has MORE (unrelated capital) -> lower-bound passes, but degraded note
    r = reconcile(local, _bal(usd1=9000.0, usdt=7000.0), [],
                  base_coin=BASE, quote_coin=QUOTE, tol=1.0, dedicated=False)
    assert r["ok"] is True and r["action"] == "proceed"
    assert any("DEGRADED" in d for d in r["discrepancies"])


def test_resumed_shared_uta_below_lowerbound_refuses():
    local = {"resumed": True, "deployed": True, "base_qty": 6000.0, "quote_qty": 4000.0}
    r = reconcile(local, _bal(usd1=5000.0, usdt=4000.0), [],   # below local base
                  base_coin=BASE, quote_coin=QUOTE, tol=1.0, dedicated=False)
    assert r["ok"] is False and r["action"] == "refuse"


# --- no local state: fresh deploy needs opt-in + a DECLARATION (Codex P0) ----

def test_no_state_without_optin_refuses_even_if_clean():
    r = reconcile(None, _bal(usdt=10000.0), [],   # clean: all USDT, no orders
                  base_coin=BASE, quote_coin=QUOTE, tol=1.0, allow_fresh=False)
    assert r["ok"] is False and r["action"] == "refuse"
    assert any("authorized" in d or "fresh" in d.lower() for d in r["discrepancies"])


def test_optin_without_declaration_refuses():
    # opt-in alone is NOT enough — balances can't be trusted to infer intent
    r = reconcile(None, _bal(usdt=10000.0), [],
                  base_coin=BASE, quote_coin=QUOTE, tol=1.0, allow_fresh=True)
    assert r["ok"] is False and r["action"] == "refuse"
    assert any("declar" in d.lower() for d in r["discrepancies"])  # declare/declaration


def test_optin_declared_usdt_match_allows_fresh_deploy():
    r = reconcile(None, _bal(usdt=10000.0), [], base_coin=BASE, quote_coin=QUOTE,
                  tol=1.0, allow_fresh=True, expect_asset="USDT", expect_amount=10000.0)
    assert r["ok"] is True and r["action"] == "fresh_deploy"


def test_optin_all_usd1_only_with_explicit_usd1_declaration():
    # Codex P0: all-USD1 + no state is ambiguous. It is accepted ONLY when the
    # operator explicitly declares USD1 + the matching amount (takes responsibility).
    bal = _bal(usd1=6000.0)
    # without declaring USD1 -> refuse (default/USDT declaration must NOT pass it)
    r0 = reconcile(None, bal, [], base_coin=BASE, quote_coin=QUOTE, tol=1.0,
                   allow_fresh=True, expect_asset="USDT", expect_amount=6000.0)
    assert r0["action"] == "refuse"
    # explicitly declaring USD1:6000 -> fresh deploy
    r1 = reconcile(None, bal, [], base_coin=BASE, quote_coin=QUOTE, tol=1.0,
                   allow_fresh=True, expect_asset="USD1", expect_amount=6000.0)
    assert r1["action"] == "fresh_deploy" and r1["ok"] is True


def test_optin_declared_amount_mismatch_refuses():
    r = reconcile(None, _bal(usdt=5000.0), [], base_coin=BASE, quote_coin=QUOTE,
                  tol=1.0, allow_fresh=True, expect_asset="USDT", expect_amount=10000.0)
    assert r["ok"] is False and r["action"] == "refuse"


def test_optin_declared_but_other_side_not_dust_refuses():
    r = reconcile(None, _bal(usd1=6000.0, usdt=4000.0), [], base_coin=BASE, quote_coin=QUOTE,
                  tol=1.0, allow_fresh=True, expect_asset="USDT", expect_amount=4000.0)
    assert r["ok"] is False and r["action"] == "refuse"


# --- open orders refuse on EVERY path (taker bot leaves none) (Codex P1) -----

def test_open_orders_refuse_when_resumed_even_if_balances_match():
    local = {"resumed": True, "deployed": True, "base_qty": 6000.0, "quote_qty": 4000.0}
    r = reconcile(local, _bal(usd1=6000.0, usdt=4000.0),
                  [{"id": "x", "symbol": "USD1/USDT"}],
                  base_coin=BASE, quote_coin=QUOTE, tol=1.0, dedicated=True)
    assert r["ok"] is False and r["action"] == "refuse"
    assert any("open order" in d.lower() for d in r["discrepancies"])


def test_open_orders_refuse_on_fresh_path():
    r = reconcile(None, _bal(usdt=10000.0), [{"id": "x"}], base_coin=BASE, quote_coin=QUOTE,
                  tol=1.0, allow_fresh=True, expect_asset="USDT", expect_amount=10000.0)
    assert r["ok"] is False and r["action"] == "refuse"


def test_clean_start_flag_reported():
    r = reconcile(None, _bal(usdt=10000.0), [], base_coin=BASE, quote_coin=QUOTE,
                  tol=1.0, allow_fresh=False)
    assert r["exchange_clean_start"] is True
    r2 = reconcile(None, _bal(usd1=6000.0, usdt=4000.0), [], base_coin=BASE,
                   quote_coin=QUOTE, tol=1.0, allow_fresh=False)
    assert r2["exchange_clean_start"] is False


# --- maker-aware: EXPECTED resting orders are no longer auto-anomaly (F1) -----
# A maker strategy leaves resting orders BY DESIGN. `expected` = the set of our
# link_ids (clientOrderId). Empty `expected` => OLD taker refuse-on-any-order.

def _order(link, side="sell", price=1.0001, qty=100.0, oid="o1"):
    return {"id": oid, "symbol": "USD1/USDT", "side": side, "price": price,
            "qty": qty, "type": "limit", "clientOrderId": link}


def test_reconcile_expected_maker_orders_proceeds():
    local = {"resumed": True, "deployed": True, "base_qty": 6000.0, "quote_qty": 4000.0}
    orders = [_order("sca-0-1"), _order("sca-1-1", side="buy", oid="o2")]
    r = reconcile(local, _bal(usd1=6000.0, usdt=4000.0), orders,
                  base_coin=BASE, quote_coin=QUOTE, tol=1.0, dedicated=True,
                  expected={"sca-0-1", "sca-1-1"})
    assert r["ok"] is True and r["action"] == "proceed"
    assert r["discrepancies"] == []


def test_reconcile_orphan_order_refuses():
    # one order is NOT in expected (foreign/orphan) -> refuse, even if balances match
    local = {"resumed": True, "deployed": True, "base_qty": 6000.0, "quote_qty": 4000.0}
    orders = [_order("sca-0-1"), _order("FOREIGN-xyz", oid="o2")]
    r = reconcile(local, _bal(usd1=6000.0, usdt=4000.0), orders,
                  base_coin=BASE, quote_coin=QUOTE, tol=1.0, dedicated=True,
                  expected={"sca-0-1"})
    assert r["ok"] is False and r["action"] == "refuse"
    assert any("open order" in d.lower() for d in r["discrepancies"])


def test_reconcile_empty_expected_preserves_taker_refuse():
    # no `expected` passed => taker semantics: ANY open order refuses (13 legacy tests)
    local = {"resumed": True, "deployed": True, "base_qty": 6000.0, "quote_qty": 4000.0}
    r = reconcile(local, _bal(usd1=6000.0, usdt=4000.0), [_order("sca-0-1")],
                  base_coin=BASE, quote_coin=QUOTE, tol=1.0, dedicated=True)
    assert r["ok"] is False and r["action"] == "refuse"
    assert any("open order" in d.lower() for d in r["discrepancies"])


def test_reconcile_balance_still_checked_with_orders():
    # expected orders present but balance is short -> balance check STILL runs -> refuse
    local = {"resumed": True, "deployed": True, "base_qty": 6000.0, "quote_qty": 4000.0}
    r = reconcile(local, _bal(usd1=5000.0, usdt=4000.0), [_order("sca-0-1")],
                  base_coin=BASE, quote_coin=QUOTE, tol=1.0, dedicated=True,
                  expected={"sca-0-1"})
    assert r["ok"] is False and r["action"] == "refuse"
    assert any("USD1" in d for d in r["discrepancies"])


def test_reconcile_proceeds_on_mid_partial_restart():
    # F2: a mid-partial slice holds BOTH base residual and quote proceeds; the local
    # summary sums both legs. Exchange shows both -> no false refuse -> proceed.
    local = {"resumed": True, "deployed": True, "base_qty": 3000.0, "quote_qty": 3000.0}
    r = reconcile(local, _bal(usd1=3000.0, usdt=3000.0), [_order("sca-0-1")],
                  base_coin=BASE, quote_coin=QUOTE, tol=1.0, dedicated=True,
                  expected={"sca-0-1"})
    assert r["ok"] is True and r["action"] == "proceed"


def test_reconcile_decides_resume_applies_ownership_split():
    # F23: reconcile DECIDES (proceed) and performs NO side-effects — it hands the
    # open_orders list back UNTOUCHED for the engine's resume to apply the ownership
    # split (re-link / cancel-orphan / refuse-foreign). reconcile never mutates/cancels.
    local = {"resumed": True, "deployed": True, "base_qty": 6000.0, "quote_qty": 4000.0}
    orders = [_order("sca-0-1"), _order("sca-1-1", side="buy", oid="o2")]
    snapshot = [dict(o) for o in orders]
    r = reconcile(local, _bal(usd1=6000.0, usdt=4000.0), orders,
                  base_coin=BASE, quote_coin=QUOTE, tol=1.0, dedicated=True,
                  expected={"sca-0-1", "sca-1-1"})
    assert r["action"] == "proceed"
    assert orders == snapshot          # inputs not mutated => no side-effects
    assert r["open_orders"] is orders  # report carries the list for resume to act on
