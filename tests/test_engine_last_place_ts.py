"""Task3: per-slice last_place_ts field — unit tests.

Tests cover:
  - _ORDER_FIELD_DEFAULTS contains "last_place_ts": None
  - _place records last_place_ts before the network call (pre-call intent)
  - _clear_slice_order resets last_place_ts to None
  - _migrate_order_fields validates last_place_ts type (None or int/float)
"""
from __future__ import annotations

import types
import pytest

from sca.live.engine import _ORDER_FIELD_DEFAULTS


# ---------------------------------------------------------------------------
# Step 1 — field default
# ---------------------------------------------------------------------------

def test_last_place_ts_in_order_field_defaults():
    assert "last_place_ts" in _ORDER_FIELD_DEFAULTS
    assert _ORDER_FIELD_DEFAULTS["last_place_ts"] is None


# ---------------------------------------------------------------------------
# Helpers shared by the remaining tests
# ---------------------------------------------------------------------------

def _make_engine_stub():
    """Return a minimal engine-like object that exercises the real methods."""
    from sca.live.engine import PaperEngine

    # We need a PaperEngine instance without going through __init__.
    # Use object.__new__ to bypass __init__, then patch the minimal attrs
    # that _place / _clear_slice_order / _migrate_order_fields touch.
    eng = object.__new__(PaperEngine)

    # Attributes that _place reads/writes
    eng.slices = []
    eng.symbol = "USD1USDT"
    eng.mode = "paper"
    eng._r1_ok = True
    eng.maker_enabled = True
    eng.anchor = 1.0
    eng._halt_reason = None
    eng._durable_halted = False

    # _persist_durable_or_halt is a no-op in these unit tests
    eng._persist_durable_or_halt = lambda: None
    # _note_reject / _reset_reject are no-ops
    eng._note_reject = lambda i: None
    eng._reset_reject = lambda i: None
    # _notify_order_placed / _notify_fill_executed are no-ops
    eng._notify_order_placed = lambda **kw: None
    eng._notify_fill_executed = lambda **kw: None
    # accounting paths (_apply_exec -> _log_event) touch these; keep minimal
    eng.events = []
    eng.persist = False
    eng._log_event = lambda *a, **k: None
    # out_dir / order_client not needed for these tests

    return eng


def _minimal_slice():
    """A dict that _place will mutate."""
    return {
        "state": "open", "qty": 100.0, "cash": 0.0,
        "sell_px": 0.0, "entry": None,
        "order_id": None, "order_link_id": None,
        "order_px": None, "order_side": None, "order_qty": None,
        "order_gen": 0, "filled_qty": 0.0, "reject_streak": 0,
        "sell_proceeds": 0.0, "qty_sold": 0.0,
        "last_place_ts": None,
    }


def _make_action(slice_idx: int, price: float = 1.0001, qty: float = 100.0):
    """Minimal action object that _place reads."""
    desired = types.SimpleNamespace(side="buy", price=price, qty=qty)
    return types.SimpleNamespace(
        slice_idx=slice_idx, desired=desired,
        live=types.SimpleNamespace(order_id=None, link_id=None),
        kind="place",
    )


def _make_client(status_class: str = "open", order_id: str = "OID1"):
    """Fake exchange client for _place."""
    def place_postonly(symbol, side, price, qty, link_id):
        return {"status_class": status_class, "id": order_id}

    return types.SimpleNamespace(place_postonly=place_postonly)


# ---------------------------------------------------------------------------
# _place pre-call recording
# ---------------------------------------------------------------------------

def test_place_records_last_place_ts_before_network_call():
    """_place must write last_place_ts = now BEFORE the network call so a
    crash between persist-intent and the actual HTTP call still preserves
    the timestamp (Codex #3 / pre-call-intent requirement)."""
    eng = _make_engine_stub()
    s = _minimal_slice()
    eng.slices = [s]

    NOW = 1_700_000_000.0
    recorded_ts_at_call = {}

    def place_postonly(symbol, side, price, qty, link_id):
        # Capture last_place_ts at the moment the network call happens
        recorded_ts_at_call["ts"] = s["last_place_ts"]
        return {"status_class": "open", "id": "OID1"}

    client = types.SimpleNamespace(place_postonly=place_postonly)
    action = _make_action(0)

    eng._place(action, client, NOW)

    # ts must already be set when the network call fires
    assert recorded_ts_at_call["ts"] == NOW
    # and still set after a successful place
    assert s["last_place_ts"] == NOW


def test_place_records_last_place_ts_on_postonly_rejected():
    """Even when the order is PostOnly-rejected, last_place_ts should be
    recorded (the INTENT to place happened; _clear_slice_order resets it
    independently, but the timestamp is set pre-call)."""
    eng = _make_engine_stub()
    s = _minimal_slice()
    eng.slices = [s]

    NOW = 1_700_000_001.0
    recorded_ts_at_call = {}

    def place_postonly(symbol, side, price, qty, link_id):
        recorded_ts_at_call["ts"] = s["last_place_ts"]
        return {"status_class": "postonly_rejected", "id": None}

    client = types.SimpleNamespace(place_postonly=place_postonly)
    eng._place(_make_action(0), client, NOW)

    assert recorded_ts_at_call["ts"] == NOW


def test_place_now_is_positional_third_arg():
    """Calling _place(action, client, now) must not raise TypeError."""
    eng = _make_engine_stub()
    eng.slices = [_minimal_slice()]
    eng._place(_make_action(0), _make_client(), 1_700_000_000.0)  # no exception


# ---------------------------------------------------------------------------
# _clear_slice_order resets last_place_ts
# ---------------------------------------------------------------------------

def test_clear_slice_order_resets_last_place_ts():
    """_clear_slice_order must set last_place_ts = None (Codex #4 clear req)."""
    eng = _make_engine_stub()
    s = _minimal_slice()
    s["last_place_ts"] = 1_700_000_000.0   # simulate a previously placed order
    s["order_id"] = "OID1"
    s["order_link_id"] = "sca-0-1"
    s["order_px"] = 1.0001
    s["order_side"] = "buy"
    s["order_qty"] = 100.0
    eng.slices = [s]

    eng._clear_slice_order(0)

    assert s["last_place_ts"] is None
    # Existing clears should still hold
    assert s["order_id"] is None
    assert s["order_link_id"] is None
    assert s["order_px"] is None


# ---------------------------------------------------------------------------
# _migrate_order_fields type-check for last_place_ts
# ---------------------------------------------------------------------------

def test_migrate_accepts_none_last_place_ts():
    """None is a valid value for last_place_ts (never placed yet)."""
    from sca.live.engine import PaperEngine
    slices = [{"last_place_ts": None}]
    result = PaperEngine._migrate_order_fields(slices, migrate=False)
    assert result is True


def test_migrate_accepts_float_last_place_ts():
    """A Unix-timestamp float must be accepted."""
    from sca.live.engine import PaperEngine
    slices = [{"last_place_ts": 1_700_000_000.0}]
    result = PaperEngine._migrate_order_fields(slices, migrate=False)
    assert result is True


def test_migrate_accepts_int_last_place_ts():
    """An integer timestamp must also be accepted."""
    from sca.live.engine import PaperEngine
    slices = [{"last_place_ts": 1_700_000_000}]
    result = PaperEngine._migrate_order_fields(slices, migrate=False)
    assert result is True


def test_migrate_rejects_string_last_place_ts():
    """A string value must cause _migrate_order_fields to return False (Codex #4)."""
    from sca.live.engine import PaperEngine
    slices = [{"last_place_ts": "2024-01-01"}]
    result = PaperEngine._migrate_order_fields(slices, migrate=False)
    assert result is False


def test_migrate_rejects_list_last_place_ts():
    """Any non-None, non-numeric value must be rejected."""
    from sca.live.engine import PaperEngine
    slices = [{"last_place_ts": []}]
    result = PaperEngine._migrate_order_fields(slices, migrate=False)
    assert result is False


# ===========================================================================
# QA-Lee invariant-cluster completion (Task3 quality review, 2026-07-01)
# ===========================================================================
# Cluster maps to 4 business invariants:
#   INV-1 crash-safety   : last_place_ts persisted == now BEFORE final persist
#   INV-2 re-place=first : every terminal/clear path resets last_place_ts -> None
#   INV-3 migrate-defends: corrupt-typed ts rejected; None/int/float accepted
#   INV-4 old-v2-compat  : a v2 snapshot missing the key resumes with ts absent,
#                          then injected lazily as None (never crashes reconcile)


# ---------------------------------------------------------------------------
# INV-1 crash-safety — KILLER for the surviving mutant.
# The existing test only proves ts is set by the time the NETWORK call fires.
# It does NOT prove ts is set BEFORE _persist_durable_or_halt(). If the two
# lines are reordered (persist first, then record), a crash between them would
# snapshot last_place_ts=None -> resume treats a live order as never-placed ->
# 24h hold is silently lost. This test spies on the PERSIST call to pin order.
# ---------------------------------------------------------------------------

def test_place_records_last_place_ts_before_persist_intent():
    """CRASH-SAFETY: last_place_ts must == now at the moment the durable
    snapshot is written (the FIRST _persist_durable_or_halt inside _place),
    because that snapshot is the crash-recovery source of truth. Recording
    the timestamp AFTER the persist would let a crash-in-between drop it."""
    eng = _make_engine_stub()
    s = _minimal_slice()
    eng.slices = [s]

    NOW = 1_700_000_042.0
    ts_at_persist = []

    def spy_persist():
        # capture last_place_ts each time the durable state is persisted
        ts_at_persist.append(s["last_place_ts"])

    eng._persist_durable_or_halt = spy_persist
    eng._place(_make_action(0), _make_client(), NOW)

    # The FIRST persist inside _place is the pre-call INTENT snapshot; at that
    # point the timestamp must already carry `now` (not None / not stale).
    assert ts_at_persist, "expected _persist_durable_or_halt to be called"
    assert ts_at_persist[0] == NOW, (
        f"last_place_ts was {ts_at_persist[0]!r} at intent-persist, expected {NOW} "
        "— recording must happen BEFORE the persist, else a crash loses the hold")


# ---------------------------------------------------------------------------
# INV-2 re-place = first — every clear path resets to None.
# _clear_slice_order is the single choke point; _place routes 3 reject classes
# through it. Parametrize all three so a future edit that special-cases one
# path (skipping the clear) is caught.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reject_class", ["postonly_rejected", "too_small",
                                          "insufficient_funds"])
def test_place_reject_paths_reset_last_place_ts(reject_class):
    """All three _place reject outcomes clear last_place_ts back to None so the
    next quote is treated as a FIRST placement (not wrongly cooled-down)."""
    eng = _make_engine_stub()
    s = _minimal_slice()
    s["last_place_ts"] = 111.0                 # stale value from a prior cycle
    eng.slices = [s]

    def place_postonly(symbol, side, price, qty, link_id):
        return {"status_class": reject_class, "id": None}

    client = types.SimpleNamespace(place_postonly=place_postonly)
    eng._place(_make_action(0), client, 1_700_000_000.0)

    assert s["last_place_ts"] is None, (
        f"{reject_class} left a stale last_place_ts -> next place would be "
        "wrongly treated as a re-quote and could be cooled down")


def test_full_fill_flip_then_clear_resets_last_place_ts():
    """END-TO-END (INV-2): a genuine FULL fill flips state THEN clears the order
    via _apply_exec_delta. _flip_state reads order_px BEFORE the clear, so the
    ordering matters; assert the completed cycle leaves last_place_ts == None so
    the rebuy leg quotes as a first placement."""
    eng = _make_engine_stub()
    s = _minimal_slice()
    # simulate a resting SELL that just fully filled
    s["state"] = "usd1"
    s["order_side"] = "sell"
    s["order_px"] = 1.0009
    s["order_qty"] = 100.0
    s["filled_qty"] = 0.0
    s["last_place_ts"] = 999.0                  # was placed earlier
    eng.slices = [s]
    # _apply_exec_delta calls _notify_fill_executed + _apply_exec; stub the notify
    eng._notify_fill_executed = lambda **kw: None

    st = {"filled": 100.0, "avg": 1.0009, "side": "sell",
          "status_class": "filled", "id": "OID1", "link_id": "sca-0-1"}
    flipped = eng._apply_exec_delta(0, st, now=1_700_000_500.0)

    assert flipped is True
    assert s["state"] == "usdt"                 # SELL completed -> flipped
    assert s["last_place_ts"] is None, (
        "full-fill flip must clear last_place_ts so the rebuy is a first placement")


# ---------------------------------------------------------------------------
# INV-3 migrate-defends — boundary values the dev tests skipped.
# ---------------------------------------------------------------------------

def test_migrate_accepts_zero_last_place_ts():
    """0 / 0.0 is a legal numeric timestamp (epoch) and must pass — a boundary
    a `> 0` style over-strict check would wrongly reject."""
    from sca.live.engine import PaperEngine
    assert PaperEngine._migrate_order_fields([{"last_place_ts": 0}], migrate=False) is True
    assert PaperEngine._migrate_order_fields([{"last_place_ts": 0.0}], migrate=False) is True


def test_migrate_accepts_bool_last_place_ts_documents_coercion():
    """PIN CURRENT BEHAVIOR: bool is a subclass of int in Python, so True/False
    PASS the isinstance(x,(int,float)) check. This is documented, not desired —
    a corrupt snapshot with last_place_ts=True yields `now - True == now - 1`
    (a silent ~1s-ago ts). Threat model = hand-corrupted snapshot only (the
    engine never writes a bool). If Task6's cooldown must reject bool, that is a
    Dev-Lee change to _migrate; this test locks the status quo so the change is
    deliberate and visible."""
    from sca.live.engine import PaperEngine
    assert PaperEngine._migrate_order_fields([{"last_place_ts": True}], migrate=False) is True
    assert PaperEngine._migrate_order_fields([{"last_place_ts": False}], migrate=False) is True


def test_migrate_rejects_string_but_keeps_other_fields_intact():
    """A bad last_place_ts fails the whole slice (return False -> fresh start),
    even when every OTHER order field is valid — the type gate is not shadowed
    by the earlier valid checks."""
    from sca.live.engine import PaperEngine
    slices = [{
        "order_id": "OID1", "order_link_id": "sca-0-1", "order_side": "buy",
        "filled_qty": 0.0, "order_gen": 1, "sell_proceeds": 0.0, "qty_sold": 0.0,
        "last_place_ts": "corrupt",
    }]
    assert PaperEngine._migrate_order_fields(slices, migrate=False) is False


# ---------------------------------------------------------------------------
# INV-4 old-v2-compat — a v2 snapshot predating this field.
# ---------------------------------------------------------------------------

def test_migrate_v2_missing_key_passes_without_injection():
    """A pre-Task3 v2 slice has NO last_place_ts key. With migrate=False the key
    is NOT injected here (only present-but-wrong-typed fields fail); the check
    `k in s` skips the absent key -> returns True. Field is injected lazily as
    None by _ensure_order_fields at maker time (verified below)."""
    from sca.live.engine import PaperEngine
    slices = [{
        "order_id": None, "order_link_id": None, "order_side": None,
        "filled_qty": 0.0, "order_gen": 0, "sell_proceeds": 0.0, "qty_sold": 0.0,
        # NOTE: no "last_place_ts" key at all (legacy v2)
    }]
    assert PaperEngine._migrate_order_fields(slices, migrate=False) is True
    assert "last_place_ts" not in slices[0]     # migrate=False must not fabricate


def test_migrate_v1_injects_last_place_ts_none():
    """A v1 snapshot (migrate=True) injects the full order-field defaults,
    including last_place_ts=None, so a pre-maker legacy state resumes clean."""
    from sca.live.engine import PaperEngine
    slices = [{"state": "usdt", "qty": 0.0, "cash": 0.0}]   # bare v1 slice
    assert PaperEngine._migrate_order_fields(slices, migrate=True) is True
    assert slices[0]["last_place_ts"] is None


def test_ensure_order_fields_injects_last_place_ts_none_for_legacy_slice():
    """_ensure_order_fields (the lazy maker-path injector) must add
    last_place_ts=None to a legacy slice that lacks it — so `now - last_place_ts`
    is never attempted against a missing key, and the slice is treated as
    'never placed' (first placement)."""
    eng = _make_engine_stub()
    legacy = {"state": "usdt", "qty": 0.0, "cash": 0.0}     # no order fields
    eng.slices = [legacy]
    eng._ensure_order_fields()
    assert legacy["last_place_ts"] is None
    # existing defaults also present (setdefault does not clobber)
    assert legacy["order_id"] is None
