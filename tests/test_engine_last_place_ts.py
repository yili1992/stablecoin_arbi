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
    # _notify_order_placed is a no-op
    eng._notify_order_placed = lambda **kw: None
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
