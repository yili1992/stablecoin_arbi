"""Mutation-driven killer tests for _topup_to_cap (QA terminal review, 2026-06-29).

Each test cites the surviving mutant it kills (focused string-replace harness on
_topup_to_cap). The baseline test_engine_topup.py suite left these 5 mutants alive
because every fixture there marks USDT at exactly $1 (quote_mark == 1.0, so `*`==`/`)
and gives existing slices cash==0 (so `wal - sum(cash)` == `wal + sum(cash)`), and
never lands headroom/deploy ON the tolerance boundary (so `<=` == `<`).

Run: PYTHONPATH=src python3 -m pytest tests/test_engine_topup_mutation_guards.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.engine import PaperEngine          # noqa: E402


def _bal(usdc_wal=0.0, usdt_wal=0.0, usdc_usd=None, usdt_usd=None):
    """Balance with INDEPENDENT wallet vs usd value so quote_mark can be != 1.0
    (off-peg). Defaults usd==wallet ($1 peg)."""
    usdc_usd = usdc_wal if usdc_usd is None else usdc_usd
    usdt_usd = usdt_wal if usdt_usd is None else usdt_usd
    total = usdc_usd + usdt_usd
    return {"account_type": "spot",
            "totals": {"equity_usd": total, "wallet_usd": total, "available_usd": total,
                       "im_usd": 0.0, "mm_usd": 0.0, "perp_upl_usd": 0.0},
            "coins": {"USDC": {"wallet": usdc_wal, "locked": 0.0, "free": usdc_wal,
                               "usd": usdc_usd, "borrow": 0.0},
                      "USDT": {"wallet": usdt_wal, "locked": 0.0, "free": usdt_wal,
                               "usd": usdt_usd, "borrow": 0.0}}}


def _usdc_slice(qty, entry=0.9998):
    return {"state": "usd1", "qty": qty, "cash": 0.0, "sell_px": 0.0, "entry": entry}


def _usdt_slice(cash):
    return {"state": "usdt", "qty": 0.0, "cash": cash, "sell_px": 0.0, "entry": None}


def _eng(tmp_path, slices, cap=1000.0, deployed_capital=None):
    eng = PaperEngine(symbol="USDCUSDT", mode="dryrun", seconds=1, csv_path=str(tmp_path / "o.csv"))
    eng.armed = True
    eng.maker_enabled = True
    eng.slices = [dict(s) for s in slices]
    eng.deployed = bool(slices)
    eng._resumed = True
    eng._max_total_alloc_usd = cap
    if deployed_capital is None:
        deployed_capital = sum(s["qty"] for s in slices) + sum(s.get("cash", 0.0) for s in slices)
    eng._deployed_capital = deployed_capital
    return eng


# --- Survivor #2: `if headroom <= tol` -> `< tol` (boundary) -----------------
def test_topup_headroom_exactly_at_tol_is_noop(tmp_path):
    """KILLS: headroom <= tol -> headroom < tol.
    slice_value == cap - tol  => headroom == tol exactly. Under `<=` this no-ops
    (idempotent at the boundary); under `<` it would proceed.

    NOTE the masking trap: with USDT at $1, headroom==tol forces deploy = min(idle,
    headroom/1.0) = tol, which the DOWNSTREAM `deploy <= tol` guard then no-ops too —
    so the headroom-boundary mutant hides behind the deploy-boundary guard. To make the
    headroom guard the SOLE discriminator we mark USDT off-peg LOW (mark 0.5): then
    headroom/mark = 1/0.5 = 2 > tol, so under `<` it WOULD deploy ~2 USDT, under `<=` no-op."""
    # base_mark for USDC: usd 999 / wallet 999 = 1.0 ; slice_value = 999*1.0 = 999
    # cap 1000 -> headroom = 1.0 == tol. USDT off-peg low: wallet 600 worth $300 -> mark 0.5.
    eng = _eng(tmp_path, [_usdc_slice(999.0)], cap=1000.0)
    eng._topup_to_cap(_bal(usdc_wal=999.0, usdt_wal=600.0, usdt_usd=300.0), [])
    assert len(eng.slices) == 1                       # headroom == tol -> NO new slice (idempotent)


# --- Survivor #3: `idle_quote = wal_quote - sum(cash)` -> `+ sum(cash)` -------
def test_topup_idle_subtracts_already_parked_cash(tmp_path):
    """KILLS: idle = wal_quote - sum(cash) -> wal_quote + sum(cash).
    An existing usdt-state slice already parks 600 USDT cash; the wallet shows that
    same 600 (it is the parked leg, NOT free idle). True idle = 600 - 600 = 0 -> no-op.
    The mutant computes 600 + 600 = 1200 idle and would re-deploy already-committed cash."""
    eng = _eng(tmp_path, [_usdc_slice(300.0), _usdt_slice(600.0)], cap=1000.0)
    # wallet USDT = 600 == the parked cash of the existing usdt slice (no free idle)
    eng._topup_to_cap(_bal(usdc_wal=300.0, usdt_wal=600.0), [])
    # headroom = 1000 - (300 + 600) = 100 > tol, BUT real idle = 600 - 600 = 0 -> no deploy
    assert len(eng.slices) == 2                       # no phantom re-deploy of parked cash
    assert eng.slices[1] == _usdt_slice(600.0)        # existing usdt slice untouched


def test_topup_idle_partial_free_over_parked(tmp_path):
    """KILLS (value, not just count): idle = wal - sum(cash) -> wal + sum(cash).
    Existing usdt slice parks 600; wallet shows 800 USDT => 200 genuinely free.
    Deploy must be bounded by REAL idle 200 (= 800 - 600), not 800+600=1400 nor the
    100 headroom-only path. headroom here = 1000-(300+600)=100, idle=200 -> min=100."""
    eng = _eng(tmp_path, [_usdc_slice(300.0), _usdt_slice(600.0)], cap=1000.0)
    eng._topup_to_cap(_bal(usdc_wal=300.0, usdt_wal=800.0), [])
    assert len(eng.slices) == 3
    # min(real_idle=200, headroom=100) = 100
    assert abs(eng.slices[2]["cash"] - 100.0) < 1e-6


# --- Survivor #4: `headroom / quote_mark` -> `headroom * quote_mark` ----------
def test_topup_headroom_divided_by_quote_mark_offpeg(tmp_path):
    """KILLS: headroom / quote_mark -> headroom * quote_mark.
    USDT off-peg HIGH: wallet 600 USDT worth $660 -> quote_mark = 1.10. headroom is in
    USD ($600). To deploy $600 worth you need 600/1.10 = 545.45 USDT, NOT 600*1.10=660.
    With ample idle the deploy amount discriminates `/` from `*`."""
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0)
    # USDT wallet 5000 (ample), valued $5500 -> mark 1.10 ; USDC at $1
    eng._topup_to_cap(_bal(usdc_wal=400.0, usdt_wal=5000.0, usdt_usd=5500.0), [])
    assert len(eng.slices) == 2
    # slice_value = 400*1.0 + 0 = 400 ; headroom = 600 (USD) ; deploy = min(idle, 600/1.10)
    # idle = 5000 (no parked cash) ; 600/1.10 = 545.4545...
    assert abs(eng.slices[1]["cash"] - (600.0 / 1.10)) < 1e-4


# --- Survivor #5: `if deploy <= tol` -> `< tol` (boundary) -------------------
def test_topup_deploy_exactly_at_tol_is_noop(tmp_path):
    """KILLS: deploy <= tol -> deploy < tol.
    Real idle == tol (1.0) while headroom is large => deploy = min(1.0, big) = 1.0 == tol.
    Under `<=` this no-ops (a $1 deploy is not worth a slice); under `<` it would append
    a $1 slice. Pin no-op."""
    # cap huge headroom: slice 100 of 1000 -> headroom 900 ; idle USDT = 1.0 == tol
    eng = _eng(tmp_path, [_usdc_slice(100.0)], cap=1000.0)
    eng._topup_to_cap(_bal(usdc_wal=100.0, usdt_wal=1.0), [])
    assert len(eng.slices) == 1                       # deploy == tol -> NO new slice


# --- Survivor #6: `_deployed_capital = prior + deploy * quote_mark` -> `/` ----
def test_topup_deployed_capital_uses_deploy_times_mark_offpeg(tmp_path):
    """KILLS: _deployed_capital prior + deploy * quote_mark -> prior + deploy / quote_mark.
    Off-peg USDT mark 1.10. deploy is a USDT amount; its USD contribution to the honest
    baseline is deploy*1.10, NOT deploy/1.10. Verify the baseline increment == deploy*mark."""
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0, deployed_capital=400.0)
    base = eng._deployed_capital
    eng._topup_to_cap(_bal(usdc_wal=400.0, usdt_wal=5000.0, usdt_usd=5500.0), [])
    deploy = eng.slices[1]["cash"]                    # the USDT amount actually deployed
    mark = 5500.0 / 5000.0                            # 1.10
    assert abs(eng._deployed_capital - (base + deploy * mark)) < 1e-4
    # and NOT the `/` mutant
    assert abs(eng._deployed_capital - (base + deploy / mark)) > 1.0


# --- Survivor #1 (equivalent-for-outcome) but lock the no-op contract --------
def test_topup_cap_zero_is_noop(tmp_path):
    """Contract lock for cap == 0 (deploy-nothing freeze). headroom = 0 - slice_value <= 0
    => no-op regardless of the `cap < 0` vs `cap <= 0` branch. Documents the intended
    'zero cap deploys nothing' behaviour so a future refactor can't silently deploy."""
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=0.0)
    eng._topup_to_cap(_bal(usdc_wal=400.0, usdt_wal=600.0), [])
    assert len(eng.slices) == 1                       # zero cap -> never deploys


# --- existing-slice cost-basis immutability under off-peg (defense in depth) --
def test_topup_existing_slice_entry_qty_untouched_field_by_field(tmp_path):
    """The existing slice's cost basis (entry) and qty must be byte-identical after a
    top-up. Asserts FIELD-BY-FIELD (not == dict) so a future field-mutating refactor is
    caught even if dict-eq is loosened."""
    orig = _usdc_slice(400.0, entry=0.99975)
    eng = _eng(tmp_path, [orig], cap=1000.0)
    eng._topup_to_cap(_bal(usdc_wal=400.0, usdt_wal=600.0), [])
    after = eng.slices[0]
    assert after["state"] == "usd1"
    assert after["qty"] == 400.0
    assert after["entry"] == 0.99975                  # cost basis frozen
    assert after["cash"] == 0.0


# --- INVARIANT: idempotent AFTER the top-up slice has filled (flipped to sell) -
def test_topup_noop_after_topup_slice_filled_and_restart(tmp_path):
    """The dangerous double-deploy path. First restart deployed 600 USDT as a usdt slice;
    that BUY then FILLED -> the slice is now usd1 (holds ~600 USDC, USDT spent). On the
    NEXT restart the wallet shows ~1000 USDC and ~0 USDT. slice_value ~ cap -> headroom ~ 0
    AND idle USDT ~ 0 -> MUST be a no-op (never re-deploy the same headroom twice).

    This is the scenario the brief flags: 'when the new slice has filled into a sell state
    after restart, headroom still ~0'. Two independent guards (headroom AND idle) must hold."""
    # post-fill state: original 400-USDC slice + the topped-up leg now holding 600 USDC
    eng = _eng(tmp_path, [_usdc_slice(400.0, entry=0.9998),
                          _usdc_slice(600.0, entry=1.0000)], cap=1000.0)
    # wallet now ~1000 USDC, ~0 free USDT (it was spent buying the 600 USDC)
    eng._topup_to_cap(_bal(usdc_wal=1000.0, usdt_wal=0.0), [])
    assert len(eng.slices) == 2                        # NO third slice — fully deployed
    # and even a residual dust of idle USDT below tol must not trigger
    eng._topup_to_cap(_bal(usdc_wal=1000.0, usdt_wal=0.5), [])
    assert len(eng.slices) == 2


def test_topup_noop_when_overcap_after_fill(tmp_path):
    """If marks drift so existing slice value EXCEEDS cap (slice_value > cap), headroom is
    negative -> no-op. Guards against a deploy on negative headroom (cap-bounded invariant
    holds on the over-cap side too)."""
    # 400 USDC + 700 USDC slices => 1100 slice_value > 1000 cap
    eng = _eng(tmp_path, [_usdc_slice(400.0), _usdc_slice(700.0)], cap=1000.0)
    eng._topup_to_cap(_bal(usdc_wal=1100.0, usdt_wal=500.0), [])
    assert len(eng.slices) == 2                        # over cap -> never deploys more
