"""Tests for the maker order client (sca.live.orders.MakerOrderClient) — Phase 3a, Task 2.

The ONLY file allowed to cross the no-order boundary. It mirrors ``BybitPrivateClient``
construction (spot + Unified + sandbox) but adds NO order method to the read-only
client — ``test_client_exposes_no_order_methods`` (test_bybit_client.py:133) stays green.

ISOLATION: zero network. A FakeCcxt module is injected; creds come from injected
dicts. Real ccxt EXCEPTION classes are raised by the fake to exercise the genuine
classification paths (real error hierarchy over mocks). Nothing reads the real env,
config, or talks to an exchange.

Grounded ccxt 4.5.54 facts baked into the assertions:
- place via create_order(sym,'limit',side,qty,price,{postOnly:True,isLeverage:0,clientOrderId:link})
- fetch_order_state = fetch_open_orders(params orderLinkId) THEN
  fetch_canceled_and_closed_orders(params orderLinkId) — NEVER fetch_order/fetch_closed_order
- amend = qty-only, NO postOnly/timeInForce; refuses price change + partial fills
- dup clientOrderId retCodes {170141, 12141, 30001} ALL -> idempotent fetch-state path
- max_order_usd HARD-asserts (raises); refuse to CONSTRUCT or place on mainnet

Run: PYTHONPATH=src python3 -m pytest tests/test_orders.py -q
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import ccxt  # noqa: E402  (real exception hierarchy)
import pytest  # noqa: E402

from sca.live import orders as om  # noqa: E402

SYMBOL = "USD1/USDT"
_GOOD_CFG = {"api_key_env": "K", "api_secret_env": "S", "max_order_usd": 2000}
_GOOD_ENV = {"K": "key-123", "S": "secret-456"}


# --- canned ccxt-parsed order rows (top-level + raw V5 ``info``) -------------
def _order(*, oid="ord1", link="sca-0-1", side="sell", price=1.0005, amount=1500.0,
           filled=0.0, remaining=None, average=None, status="open",
           order_status="New", cum_exec="0", leaves=None, avg_price="",
           reject="EC_NoError"):
    if remaining is None and amount is not None and filled is not None:
        remaining = amount - filled
    if leaves is None:
        leaves = "" if remaining is None else str(remaining)
    return {
        "id": oid, "clientOrderId": link, "symbol": SYMBOL, "side": side,
        "price": price, "amount": amount, "filled": filled, "remaining": remaining,
        "average": average, "status": status,
        "info": {"orderId": oid, "orderLinkId": link, "orderStatus": order_status,
                 "cumExecQty": cum_exec, "leavesQty": leaves, "avgPrice": avg_price,
                 "rejectReason": reject, "side": side.capitalize()},
    }


_MARKET = {"symbol": SYMBOL, "precision": {"price": 0.0001, "amount": 0.001},
           "limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}}}


class FakeExchange:
    def __init__(self, config):
        self.config = config
        self.calls = []
        self.sandbox = False
        # programmable responses
        self.market_data = dict(_MARKET)
        self.create_result = None
        self.create_seq = None            # list of Exception|dict for backoff tests
        self.open_orders = []
        self.terminal_orders = []
        self.edit_result = None
        self.cancel_result = None
        self.balance_result = None
        self.balance_seq = None
        self.time_syncs = 0

    def set_sandbox_mode(self, v):
        self.sandbox = v
        self.calls.append(("set_sandbox_mode", v))

    def load_markets(self, *a, **k):
        self.calls.append(("load_markets",))
        return {SYMBOL: self.market_data}

    def market(self, symbol):
        self.calls.append(("market", symbol))
        return self.market_data

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        self.calls.append(("create_order", {"symbol": symbol, "type": type, "side": side,
                                             "amount": amount, "price": price,
                                             "params": params or {}}))
        if self.create_seq is not None:
            item = self.create_seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if isinstance(self.create_result, Exception):
            raise self.create_result
        return self.create_result

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        self.calls.append(("fetch_open_orders", symbol, params or {}))
        return list(self.open_orders)

    def fetch_canceled_and_closed_orders(self, symbol=None, since=None, limit=None, params=None):
        self.calls.append(("fetch_canceled_and_closed_orders", symbol, params or {}))
        return list(self.terminal_orders)

    def edit_order(self, id, symbol, type, side, amount=None, price=None, params=None):
        self.calls.append(("edit_order", {"id": id, "symbol": symbol, "type": type,
                                           "side": side, "amount": amount, "price": price,
                                           "params": params or {}}))
        if isinstance(self.edit_result, Exception):
            raise self.edit_result
        return self.edit_result

    def cancel_order(self, id, symbol=None, params=None):
        self.calls.append(("cancel_order", id, params or {}))
        if isinstance(self.cancel_result, Exception):
            raise self.cancel_result
        return self.cancel_result

    def fetch_balance(self, params=None):
        self.calls.append(("fetch_balance", params or {}))
        if self.balance_seq is not None:
            item = self.balance_seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if isinstance(self.balance_result, Exception):
            raise self.balance_result
        return self.balance_result

    def load_time_difference(self, params=None):
        self.calls.append(("load_time_difference", params or {}))
        self.time_syncs += 1
        return -1001

    # The Filled-only paths that hide cancels/rejects MUST NEVER be called.
    def fetch_order(self, *a, **k):
        self.calls.append(("fetch_order", a))
        raise AssertionError("fetch_order must never be called (Filled-only / hides cancels)")

    def fetch_closed_order(self, *a, **k):
        self.calls.append(("fetch_closed_order", a))
        raise AssertionError("fetch_closed_order must never be called (orderStatus=Filled only)")

    def fetch_closed_orders(self, *a, **k):
        self.calls.append(("fetch_closed_orders", a))
        raise AssertionError("fetch_closed_orders must never be called (Filled-only)")


class FakeCcxt:
    def __init__(self):
        self.last = None
        self.last_kind = None

    def bybit(self, config):
        self.last = FakeExchange(config)
        self.last_kind = "bybit"
        return self.last

    def bitget(self, config):
        self.last = FakeExchange(config)
        self.last_kind = "bitget"
        return self.last


def _mk(**over):
    fake = FakeCcxt()
    kwargs = dict(ccxt_module=fake, live_cfg=_GOOD_CFG, env=_GOOD_ENV)
    kwargs.update(over)
    client = om.MakerOrderClient(**kwargs)
    client._sleep = lambda _s: None         # no real backoff sleeping in tests
    return client, fake.last


def _names(ex):
    return [c[0] for c in ex.calls]


def _raw_uta_balance(**coins):
    """A raw ccxt bybit UTA ``fetch_balance`` result (only the fields normalize_balance reads)."""
    total = sum(coins.values())
    rows = [{"coin": c, "walletBalance": str(v), "locked": "0", "usdValue": str(v),
             "equity": str(v), "spotBorrow": "0"} for c, v in coins.items()]
    return {"info": {"result": {"list": [{
        "accountType": "UNIFIED", "totalEquity": str(total),
        "totalAvailableBalance": str(total), "totalWalletBalance": str(total),
        "totalInitialMargin": "0", "totalMaintenanceMargin": "0", "coin": rows}]}}}


def test_balance_exposes_normalized_uta_wallet():
    # REGRESSION (live-only crash, 2026-06-20): engine.reconcile_orders sizes its pool from
    # ``client.balance()``, but ONLY the test FakeOrderClients implemented .balance() — the
    # real MakerOrderClient had no such method, so the LIVE path (reconcile_orders runs only
    # when maker_enabled) raised AttributeError and spun an endless reconnect loop. The real
    # client must expose .balance() returning the same normalized shape as
    # BybitPrivateClient.get_wallet_balance, fetched from the UNIFIED account.
    client, ex = _mk()
    ex.balance_result = _raw_uta_balance(USDT=1000.20, USD1=0.0)
    bal = client.balance()
    assert abs(bal["coins"]["USDT"]["wallet"] - 1000.20) < 1e-9
    assert abs(bal["coins"]["USDT"]["free"] - 1000.20) < 1e-9
    assert ("fetch_balance", {"type": "unified"}) in ex.calls


def test_balance_invalid_nonce_syncs_time_then_retries():
    client, ex = _mk()
    ex.balance_seq = [
        ccxt.InvalidNonce(
            'bybit {"retCode":10002,"retMsg":"invalid request, please check your server '
            'timestamp or recv_window param: req_timestamp[1781948271704],'
            'server_timestamp[1781948270703],recv_window[5000]"}'
        ),
        _raw_uta_balance(USDT=42.0),
    ]
    out = client.balance()
    assert out["coins"]["USDT"]["wallet"] == 42.0
    assert _names(ex).count("fetch_balance") == 2
    assert _names(ex).count("load_time_difference") == 1


# --- construction (spot + Unified; MAINNET only, D14) -----------------------

def test_constructs_spot_unified_mainnet():
    # D14: live == real MAINNET — spot/Unified, rate-limited, secrets never logged.
    # There is no testnet/sandbox gate and no per-order cap.
    client, ex = _mk()
    cfg = ex.config
    assert cfg["options"]["defaultType"] == "spot"
    assert cfg["options"]["recvWindow"] == 10_000
    assert cfg["options"]["adjustForTimeDifference"] is True
    assert cfg["enableRateLimit"] is True
    assert cfg.get("verbose") is False
    assert not hasattr(client, "max_order_usd")     # per-order cap removed (D14)
    assert not hasattr(client, "testnet")           # no testnet/sandbox gate (D14)


def test_missing_credentials_raises():
    # a live client with no keys raises a clear RuntimeError at construction (no downgrade).
    fake = FakeCcxt()
    with pytest.raises(RuntimeError):
        om.MakerOrderClient(ccxt_module=fake, live_cfg=_GOOD_CFG, env={})


# --- adapter-driven construction (Phase 3): Bybit default vs Bitget by symbol --
# The maker client builds the venue ccxt instance via the per-symbol ExchangeAdapter
# (orders.py:89 was hardcoded ``BybitAdapter()``). Bybit stays byte-identical (default /
# no symbol => BybitAdapter => mod.bybit, raw clientOrderId). A Bitget symbol routes to
# mod.bitget with the passphrase + sanitized clientOid. feedback_shared_mapping_no_duplicate.

from sca.live.exchanges.bybit import BybitAdapter         # noqa: E402
from sca.live.exchanges.bitget import BitgetAdapter       # noqa: E402


def test_default_construction_uses_bybit_adapter_byte_identical():
    # zero-change: no symbol => BybitAdapter => mod.bybit(...) with the legacy config.
    client, ex = _mk()
    fake = FakeCcxt()
    om.MakerOrderClient(ccxt_module=fake, live_cfg=_GOOD_CFG, env=_GOOD_ENV)
    assert fake.last_kind == "bybit"
    assert isinstance(client.adapter, BybitAdapter)


def test_bybit_order_params_remain_raw_clientorderid():
    # Bybit byte-identity for the maker order params (raw clientOrderId, isLeverage:0).
    client, ex = _mk()
    ex.create_result = _order(link="sca-0-1", side="sell", price=1.0005, amount=1500.0)
    client.place_postonly(SYMBOL, "sell", 1.0005, 1500.0, "sca-0-1")
    p = next(c for c in ex.calls if c[0] == "create_order")[1]["params"]
    assert p == {"postOnly": True, "isLeverage": 0, "clientOrderId": "sca-0-1"}


def _mk_bitget(**over):
    """Build a MakerOrderClient routed to Bitget via an injected BitgetAdapter +
    Bitget creds (key/secret/passphrase). Returns (client, fake_exchange)."""
    fake = FakeCcxt()
    cfg = {}                          # default env names -> per-exchange routing
    env = {"BITGET_API_KEY": "bg-k", "BITGET_API_SECRET": "bg-s",
           "BITGET_API_PASSPHRASE": "bg-pass"}
    kwargs = dict(ccxt_module=fake, live_cfg=cfg, env=env,
                  adapter=BitgetAdapter(), exchange="bitget")
    kwargs.update(over)
    client = om.MakerOrderClient(**kwargs)
    client._sleep = lambda _s: None
    return client, fake.last, fake


def test_bitget_construction_uses_bitget_ctor_with_passphrase():
    client, ex, fake = _mk_bitget()
    assert fake.last_kind == "bitget"
    assert isinstance(client.adapter, BitgetAdapter)
    cfg = ex.config
    assert cfg["apiKey"] == "bg-k" and cfg["secret"] == "bg-s"
    assert cfg["password"] == "bg-pass"               # OKX-family passphrase threaded in
    assert cfg["verbose"] is False                    # secrets never logged
    assert cfg["options"]["defaultType"] == "spot"    # spot account


def test_bitget_place_uses_sanitized_clientoid_and_postonly_force():
    # Bitget order params: postOnly + clientOid is the SANITIZED link (hyphens illegal).
    client, ex, _ = _mk_bitget()
    ex.create_result = _order(link="scaX0X1", side="sell", price=1.0005, amount=1500.0)
    client.place_postonly("USDC/USDT", "sell", 1.0005, 1500.0, "sca-0-1")
    p = next(c for c in ex.calls if c[0] == "create_order")[1]["params"]
    assert p["postOnly"] is True
    assert p["clientOid"] == "scaX0X1"                 # sanitized; raw "sca-0-1" rejected
    assert "isLeverage" not in p                       # Bybit-only field, not on Bitget


def test_bitget_balance_uses_adapter_spot_shape():
    # the order client's balance() (engine sizes the pool from it) must return the Bitget
    # SPOT balance via the adapter — NOT a Bybit UTA fetch_balance({"type":"unified"}).
    client, ex, _ = _mk_bitget()
    ex.balance_result = {
        "USDT": {"free": 900.0, "used": 100.0, "total": 1000.0},
        "USDC": {"free": 0.0, "used": 0.0, "total": 0.0},
        "info": {},
    }
    bal = client.balance()
    assert bal["account_type"] == "spot"
    assert bal["coins"]["USDT"]["wallet"] == 1000.0
    # never the Bybit UTA call shape
    assert ("fetch_balance", {"type": "unified"}) not in ex.calls


def test_bitget_missing_passphrase_still_constructs_but_no_password():
    # a Bitget deploy that forgot the passphrase still builds (key/secret present); the
    # passphrase is simply absent (a signed call will later fail loudly at the venue).
    fake = FakeCcxt()
    env = {"BITGET_API_KEY": "bg-k", "BITGET_API_SECRET": "bg-s"}  # no passphrase
    client = om.MakerOrderClient(ccxt_module=fake, live_cfg={}, env=env,
                                 adapter=BitgetAdapter(), exchange="bitget")
    assert "password" not in fake.last.config           # omitted when unset


# --- market meta ------------------------------------------------------------

def test_market_meta_reads_tick_lot_min():
    client, ex = _mk()
    meta = client.market_meta(SYMBOL)
    assert meta == {"tick": 0.0001, "lot": 0.001, "min_qty": 0.001, "min_cost": 5.0}


def test_market_meta_loads_markets_first():
    # P1-5 — ccxt's ex.market(symbol) raises if markets aren't loaded yet; the FIRST live
    # reconcile would crash. market_meta MUST call load_markets() before reading the market.
    client, ex = _mk()
    client.market_meta(SYMBOL)
    names = _names(ex)
    assert "load_markets" in names
    assert names.index("load_markets") < names.index("market")


# --- place_postonly: correct ccxt call + snapped price ----------------------

def test_place_postonly_builds_correct_ccxt_call():
    client, ex = _mk()
    ex.create_result = _order(link="sca-0-1", side="sell", price=1.0005, amount=1500.0)
    client.place_postonly(SYMBOL, "sell", 1.0005, 1500.0, "sca-0-1")
    call = next(c for c in ex.calls if c[0] == "create_order")[1]
    assert call["symbol"] == SYMBOL and call["type"] == "limit"
    assert call["side"] == "sell" and call["amount"] == 1500.0 and call["price"] == 1.0005
    p = call["params"]
    assert p["postOnly"] is True and p["isLeverage"] == 0 and p["clientOrderId"] == "sca-0-1"
    assert "timeInForce" not in p and "GTC" not in p  # ccxt sets PostOnly itself; never GTC


def test_place_passes_snapped_price_not_ccxt_round():
    # the price/qty we pass are PRE-snapped by order_recon.quantize_*; the client must
    # forward them verbatim and NOT call ccxt price_to_precision (which rounds/can cross).
    client, ex = _mk()
    ex.create_result = _order(price=1.0003, amount=1000.0)

    def _boom(*a, **k):
        raise AssertionError("must NOT call price_to_precision (rounds, can cross)")

    ex.price_to_precision = _boom
    client.place_postonly(SYMBOL, "sell", 1.0003, 1000.0, "sca-1-0")
    call = next(c for c in ex.calls if c[0] == "create_order")[1]
    assert call["price"] == 1.0003 and call["amount"] == 1000.0


def test_place_postonly_no_per_order_cap():
    # D14 — there is NO per-order notional cap: a large order is forwarded verbatim to the
    # exchange (size is the ladder's alloc x fraction; total bounded by max_total_alloc_usd).
    client, ex = _mk()
    ex.create_result = _order(side="buy", price=1.0, amount=2500.0, status="open")
    client.place_postonly(SYMBOL, "buy", 1.0, 2500.0, "sca-2-0")
    call = next(c for c in ex.calls if c[0] == "create_order")[1]
    assert call["amount"] == 2500.0                  # forwarded verbatim (no cap clamp/assert)


# --- PostOnly reject is NOT an error (both grounded forms) ------------------

def test_postonly_reject_returned_order_classified_not_error():
    # form (a): create_order RETURNS a canceled/rejected order + rejectReason
    client, ex = _mk()
    ex.create_result = _order(side="sell", status="rejected", order_status="Rejected",
                              reject="EC_PostOnlyWillTakeLiquidity")
    out = client.place_postonly(SYMBOL, "sell", 1.0005, 1500.0, "sca-0-1")
    assert out["status_class"] == "postonly_rejected"
    assert out["reject_reason"] == "EC_PostOnlyWillTakeLiquidity"


def test_postonly_reject_raised_exception_classified_not_error():
    # form (b): ccxt RAISES InvalidOrder for the post-only-would-cross case
    client, ex = _mk()
    ex.create_result = ccxt.InvalidOrder(
        'bybit {"retCode":170218,"retMsg":"Order would immediately take liquidity (PostOnly)"}')
    out = client.place_postonly(SYMBOL, "sell", 1.0005, 1500.0, "sca-0-1")
    assert out["status_class"] == "postonly_rejected"


def test_postonly_170218_and_message_match_classified_rejected():
    # P1-6 — Bybit retCode 170218 "LIMIT-MAKER order is rejected due to invalid price"
    # maps to InvalidOrder; classify it by CODE (even when the message has no postonly
    # keyword), and classify any 'limit-maker' message — including a plain ExchangeError
    # (not InvalidOrder/BadRequest) — as postonly_rejected, never re-raised.
    # (a) classified purely by retCode 170218 (message carries no postonly substring)
    client, ex = _mk()
    ex.create_result = ccxt.InvalidOrder(
        'bybit {"retCode":170218,"retMsg":"rejected due to invalid price"}')
    out = client.place_postonly(SYMBOL, "sell", 1.0005, 1500.0, "sca-0-1")
    assert out["status_class"] == "postonly_rejected"
    # (b) a plain ExchangeError (no parseable retCode) whose message matches 'limit-maker'
    client, ex = _mk()
    ex.create_result = ccxt.ExchangeError("bybit limit-maker order rejected: invalid price")
    out = client.place_postonly(SYMBOL, "sell", 1.0005, 1500.0, "sca-0-1")
    assert out["status_class"] == "postonly_rejected"


def test_min_size_invalid_order_classified_too_small_not_retried():
    # F19 — below-minimum qty/cost -> 'too_small', logged-skipped, NEVER hot-retried.
    client, ex = _mk()
    ex.create_result = ccxt.InvalidOrder(
        'bybit {"retCode":170140,"retMsg":"Order value exceeded lower limit (minimum)"}')
    out = client.place_postonly(SYMBOL, "buy", 1.0, 3.0, "sca-3-0")
    assert out["status_class"] == "too_small"
    assert _names(ex).count("create_order") == 1     # single attempt, not retried


def test_dup_linkid_retcodes_all_idempotent():
    # C-P2#16 — 170141 (InvalidOrder) AND 12141 (BadRequest) AND 30001 (BadRequest
    # "order_link_id is repeated") ALL -> idempotent "already placed -> fetch state" path.
    cases = [
        ccxt.InvalidOrder('bybit {"retCode":170141,"retMsg":"order_link_id exist"}'),
        ccxt.BadRequest('bybit {"retCode":12141,"retMsg":"duplicate clientOrderId"}'),
        ccxt.BadRequest('bybit {"retCode":30001,"retMsg":"order_link_id is repeated"}'),
    ]
    for exc in cases:
        client, ex = _mk()
        ex.create_result = exc
        ex.open_orders = [_order(link="sca-0-1", order_status="New")]  # truth on exchange
        out = client.place_postonly(SYMBOL, "sell", 1.0005, 1500.0, "sca-0-1")
        assert out["status_class"] == "open"          # learned truth, no double-fill
        assert out["link_id"] == "sca-0-1"
        assert "fetch_open_orders" in _names(ex)       # went to fetch-state path


# --- fetch_order_state: open-then-terminal by link, never Filled-only -------

def test_fetch_state_open_by_linkid_then_terminal_by_linkid():
    # OPEN: present in fetch_open_orders -> never queries terminal
    client, ex = _mk()
    ex.open_orders = [_order(link="sca-0-1", order_status="PartiallyFilled",
                             cum_exec="500", filled=500.0, amount=1500.0)]
    st = client.fetch_order_state(SYMBOL, "ord1", link_id="sca-0-1")
    assert st["status_class"] == "open" and st["filled"] == 500.0
    oo = next(c for c in ex.calls if c[0] == "fetch_open_orders")
    assert oo[2] == {"orderLinkId": "sca-0-1"}
    assert "fetch_canceled_and_closed_orders" not in _names(ex)

    # TERMINAL: absent from open -> queries terminal by link
    client, ex = _mk()
    ex.open_orders = []
    ex.terminal_orders = [_order(link="sca-0-1", order_status="Filled", status="closed",
                                 cum_exec="1500", filled=1500.0, leaves="0",
                                 avg_price="1.0005", average=1.0005)]
    st = client.fetch_order_state(SYMBOL, "ord1", link_id="sca-0-1")
    assert st["status_class"] == "filled" and st["filled"] == 1500.0 and st["avg"] == 1.0005
    tc = next(c for c in ex.calls if c[0] == "fetch_canceled_and_closed_orders")
    assert tc[2] == {"orderLinkId": "sca-0-1"}


def test_fetch_terminal_state_covers_cancelled_and_rejected():
    # C-P0#2 — a cancelled-partial AND a rejected order are observable via the terminal
    # endpoint's orderStatus + rejectReason (the Filled-only path would hide them).
    client, ex = _mk()
    ex.open_orders = []
    ex.terminal_orders = [_order(link="sca-0-1", order_status="Cancelled", status="canceled",
                                 cum_exec="600", filled=600.0, leaves="0")]
    st = client.fetch_order_state(SYMBOL, "ord1", link_id="sca-0-1")
    assert st["status_class"] == "cancelled" and st["filled"] == 600.0  # cancelled-partial visible

    client, ex = _mk()
    ex.open_orders = []
    ex.terminal_orders = [_order(link="sca-0-1", order_status="Rejected", status="rejected",
                                 reject="EC_PostOnlyWillTakeLiquidity", cum_exec="0")]
    st = client.fetch_order_state(SYMBOL, "ord1", link_id="sca-0-1")
    assert st["status_class"] == "postonly_rejected"
    assert st["reject_reason"] == "EC_PostOnlyWillTakeLiquidity"


def test_fetch_state_never_calls_filled_only_closed_path():
    # C-P0#1/#2 — never fetch_order / fetch_closed_order / fetch_closed_orders.
    client, ex = _mk()
    ex.open_orders = []
    ex.terminal_orders = [_order(link="sca-0-1", order_status="Filled", status="closed",
                                 cum_exec="1500", filled=1500.0, leaves="0")]
    client.fetch_order_state(SYMBOL, "ord1", link_id="sca-0-1")
    called = _names(ex)
    assert "fetch_order" not in called
    assert "fetch_closed_order" not in called
    assert "fetch_closed_orders" not in called


def test_fetch_state_asserts_filled_finite():
    client, ex = _mk()
    ex.open_orders = []
    ex.terminal_orders = [_order(link="sca-0-1", order_status="Filled", status="closed",
                                 filled=None, cum_exec="nan", leaves="0")]
    with pytest.raises(AssertionError):
        client.fetch_order_state(SYMBOL, "ord1", link_id="sca-0-1")


def test_fetch_state_by_link_id_only_when_id_absent():
    # F14 — crash-resume: an order whose id was never persisted is fetched by link alone.
    client, ex = _mk()
    ex.open_orders = [_order(link="sca-7-2", order_status="New")]
    client.fetch_order_state(SYMBOL, None, link_id="sca-7-2")
    oo = next(c for c in ex.calls if c[0] == "fetch_open_orders")
    assert oo[2] == {"orderLinkId": "sca-7-2"}        # by link only, no orderId

    # by id when no link: params carry orderId
    client, ex = _mk()
    ex.open_orders = [_order(oid="ord9", link=None, order_status="New")]
    client.fetch_order_state(SYMBOL, "ord9")
    oo = next(c for c in ex.calls if c[0] == "fetch_open_orders")
    assert oo[2] == {"orderId": "ord9"}


# --- fetch_open: full list, each carries clientOrderId ----------------------

def test_fetch_open_exposes_client_order_id():
    # F5 — match_live_orders needs each open row to carry our link (clientOrderId).
    client, ex = _mk()
    ex.open_orders = [_order(oid="o1", link="sca-0-1", side="sell", price=1.0005,
                             amount=1500.0, filled=500.0, cum_exec="500"),
                      _order(oid="o2", link="sca-1-0", side="buy", price=0.9998,
                             amount=1000.0)]
    rows = client.fetch_open(SYMBOL)
    assert [r["link_id"] for r in rows] == ["sca-0-1", "sca-1-0"]
    # leaves (remaining) + cumExecQty (filled) preserved for the pure matcher/diff
    assert rows[0]["side"] == "sell" and rows[0]["filled"] == 500.0
    assert rows[0]["remaining"] == 1000.0


# --- amend: qty-only, no TIF/postOnly, refuses price & partials -------------

def test_amend_qty_only_no_tif_no_postonly():
    client, ex = _mk()
    ex.open_orders = [_order(link="sca-0-0", order_status="New", filled=0.0,
                             amount=1500.0)]
    client.amend(SYMBOL, "ord1", link_id="sca-0-0", qty=1200.0)
    ed = next(c for c in ex.calls if c[0] == "edit_order")[1]
    assert ed["amount"] == 1200.0
    assert ed["params"].get("orderLinkId") == "sca-0-0"
    assert "postOnly" not in ed["params"] and "timeInForce" not in ed["params"]


def test_amend_refuses_price_change():
    client, ex = _mk()
    with pytest.raises(ValueError):
        client.amend(SYMBOL, "ord1", link_id="sca-0-0", qty=1200.0, price=1.0006)
    assert "edit_order" not in _names(ex)             # a re-price is cancel+recreate


def test_amend_refuses_partially_filled_order():
    # F8 — never amend a partial (Bybit amend sets TOTAL qty -> corrupts leaves accounting).
    client, ex = _mk()
    ex.open_orders = [_order(link="sca-0-0", order_status="PartiallyFilled",
                             cum_exec="500", filled=500.0, amount=1500.0)]
    with pytest.raises(ValueError):
        client.amend(SYMBOL, "ord1", link_id="sca-0-0", qty=1200.0)
    assert "edit_order" not in _names(ex)


def test_amend_total_qty_leaves_semantics():
    # F8 — amend sets total qty; client re-polls fetch_order_state to verify new leaves.
    client, ex = _mk()
    ex.open_orders = [_order(link="sca-0-0", order_status="New", filled=0.0,
                             amount=1200.0, leaves="1200", remaining=1200.0)]
    st = client.amend(SYMBOL, "ord1", link_id="sca-0-0", qty=1200.0)
    assert st["remaining"] == 1200.0                  # re-polled leaves verified
    # re-poll happened AFTER the edit
    assert _names(ex).count("fetch_open_orders") >= 2  # pre-check + verify


# --- cancel -----------------------------------------------------------------

def test_cancel_calls_ccxt_cancel_order():
    client, ex = _mk()
    ex.cancel_result = _order(link="sca-0-0", order_status="Cancelled", status="canceled",
                              cum_exec="0", leaves="0")
    st = client.cancel(SYMBOL, "ord1", link_id="sca-0-0")
    co = next(c for c in ex.calls if c[0] == "cancel_order")
    assert co[1] == "ord1" and co[2].get("orderLinkId") == "sca-0-0"
    assert st["status_class"] == "cancelled"


# --- 429 backoff ------------------------------------------------------------

def test_rate_limit_backoff_retries_then_succeeds():
    client, ex = _mk()
    waits = []
    client._sleep = lambda s: waits.append(s)
    good = _order(link="sca-0-1", order_status="New")
    ex.create_seq = [ccxt.RateLimitExceeded("429"), ccxt.DDoSProtection("429"), good]
    out = client.place_postonly(SYMBOL, "sell", 1.0005, 1500.0, "sca-0-1")
    assert out["status_class"] == "open"
    assert _names(ex).count("create_order") == 3       # 2 retries then success
    assert len(waits) == 2 and waits[0] >= 1.0         # exponential backoff applied
    assert all(w <= om.BACKOFF_CAP for w in waits)     # capped


def test_invalid_nonce_syncs_time_then_retries_private_call():
    # REGRESSION (live reconnect loop, 2026-06-20): Bybit retCode 10002 maps to
    # ccxt.InvalidNonce when the signed timestamp is outside the recv_window. The maker
    # client must refresh CCXT's time difference and retry inside the bounded private-REST
    # wrapper, rather than letting the engine's generic websocket reconnect loop spin.
    client, ex = _mk()
    waits = []
    client._sleep = lambda s: waits.append(s)
    good = _order(link="sca-0-1", order_status="New")
    ex.create_seq = [
        ccxt.InvalidNonce(
            'bybit {"retCode":10002,"retMsg":"invalid request, please check your server '
            'timestamp or recv_window param: req_timestamp[1781948271704],'
            'server_timestamp[1781948270703],recv_window[5000]"}'
        ),
        good,
    ]
    out = client.place_postonly(SYMBOL, "sell", 1.0005, 1500.0, "sca-0-1")
    assert out["status_class"] == "open"
    assert _names(ex).count("create_order") == 2
    assert _names(ex).count("load_time_difference") == 1
    assert waits == [1.0]


def test_rate_limit_backoff_bounded_then_reraises():
    client, ex = _mk()
    client._sleep = lambda _s: None
    ex.create_seq = [ccxt.RateLimitExceeded("429")] * (om.MAX_RETRIES + 1)
    with pytest.raises(ccxt.RateLimitExceeded):
        client.place_postonly(SYMBOL, "sell", 1.0005, 1500.0, "sca-0-1")
    assert _names(ex).count("create_order") == om.MAX_RETRIES + 1


# --- insufficient funds -----------------------------------------------------

def test_insufficient_funds_skips_not_crash():
    client, ex = _mk()
    ex.create_result = ccxt.InsufficientFunds('bybit {"retCode":170131,"retMsg":"balance"}')
    out = client.place_postonly(SYMBOL, "buy", 1.0, 1000.0, "sca-4-0")
    assert out["status_class"] == "insufficient_funds"   # logged-skip, no exception


# --- mainnet place-level refusal (independent of ctor refusal) --------------

# --- branch/edge coverage (core trading path -> ~100%) ----------------------

def test_repr_redacts_secret():
    client, _ = _mk()
    r = repr(client)
    assert "secret-456" not in r and "key-123" not in r and "MAINNET" in r


def test_unknown_invalid_order_is_reraised_not_swallowed():
    # An InvalidOrder/BadRequest we can't classify must SURFACE, never be silently eaten.
    client, ex = _mk()
    ex.create_result = ccxt.InvalidOrder('bybit {"retCode":999999,"retMsg":"mystery"}')
    with pytest.raises(ccxt.InvalidOrder):
        client.place_postonly(SYMBOL, "sell", 1.0005, 1500.0, "sca-0-1")


def test_fetch_state_not_found_when_absent_everywhere():
    client, ex = _mk()
    ex.open_orders = []
    ex.terminal_orders = []
    st = client.fetch_order_state(SYMBOL, "ghost", link_id="sca-9-9")
    assert st["status_class"] == "not_found" and st["filled"] == 0.0


def test_fetch_state_plain_rejected_distinct_from_postonly():
    # a non-postonly rejection (e.g. risk/margin) classifies as 'rejected', not postonly.
    client, ex = _mk()
    ex.open_orders = []
    ex.terminal_orders = [_order(link="sca-0-1", order_status="Rejected", status="rejected",
                                 reject="EC_RejectedByRisk", cum_exec="0")]
    st = client.fetch_order_state(SYMBOL, "ord1", link_id="sca-0-1")
    assert st["status_class"] == "rejected"


def test_fetch_state_no_id_no_link_picks_sole_row():
    # _id_params={} and _pick falls back to the sole filtered row.
    client, ex = _mk()
    ex.open_orders = [_order(link="sca-0-1", order_status="New")]
    st = client.fetch_order_state(SYMBOL)
    oo = next(c for c in ex.calls if c[0] == "fetch_open_orders")
    assert oo[2] == {} and st["status_class"] == "open"


def test_normalize_tolerates_unparseable_filled():
    client, ex = _mk()
    ex.open_orders = [_order(link="sca-0-1", order_status="New", filled=None,
                             cum_exec="n/a")]
    rows = client.fetch_open(SYMBOL)
    assert rows[0]["filled"] is None                  # bad value -> None, downstream guards


def test_retry_wait_honors_retry_after_attr_then_ms_then_bad():
    # exchange Retry-After attr wins; rate_limit_reset_ms (ms) is converted; a bad attr
    # falls through to the default backoff.
    client, ex = _mk()
    waits = []
    client._sleep = lambda s: waits.append(s)
    e_attr = ccxt.RateLimitExceeded("429"); e_attr.retry_after = 3
    e_ms = ccxt.RateLimitExceeded("429 rate_limit_reset_ms: 2000")
    e_bad = ccxt.RateLimitExceeded("429"); e_bad.retry_after = object()
    e_secs = ccxt.RateLimitExceeded("429 Retry-After: 5")   # seconds form (<1000)
    ex.create_seq = [e_attr, e_ms, e_bad, e_secs,
                     _order(link="sca-0-1", order_status="New")]
    out = client.place_postonly(SYMBOL, "sell", 1.0005, 1500.0, "sca-0-1")
    assert out["status_class"] == "open"
    assert waits[0] == 3.0 and waits[1] == 2.0        # attr secs, then ms->secs
    assert waits[2] >= 1.0                             # bad attr -> default backoff
    assert waits[3] == 5.0                             # Retry-After seconds form


def test_unknown_order_status_treated_as_open_non_terminal():
    # an unrecognized orderStatus is conservatively NON-terminal (keep polling),
    # never silently treated as filled/cancelled.
    client, ex = _mk()
    ex.open_orders = [_order(link="sca-0-1", order_status="SomeFutureState")]
    rows = client.fetch_open(SYMBOL)
    assert rows[0]["status_class"] == "open"


def test_real_ccxt_module_used_when_not_injected():
    # ccxt_module=None -> builds a real ccxt.bybit (no network on construction).
    client = om.MakerOrderClient(ccxt_module=None, live_cfg=_GOOD_CFG, env=_GOOD_ENV)
    assert client.ex.__class__.__name__ == "bybit"
