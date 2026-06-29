# USDC 实盘 Top-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实盘 maker 引擎重启时保留现有仓位(按原成本挂卖),用 `cap − 现有持仓估值` 的 headroom 把闲置 USDT 部署成买单,把单 symbol 放大到 `max_total_alloc_usd`,不丢成本基准。

**Architecture:** 在 R1 gate(`_reconcile_or_refuse`)内、`reconcile()` 之前,对 resumed-deployed 状态调 `_topup_to_cap` append 一个 USDT 买 slice;headroom 受真实 idle 余额与 cap 双重封顶。配套修 rung 越界(slice 数 > 配置 rung 数时 `rungs[i]` IndexError),加 `rung_for(rungs,i)` clamp 应用于 dryrun/status/**live maker** 三处。

**Tech Stack:** Python 3, pytest;`src/sca/live/{engine,order_recon}.py` + `src/sca/strategy_rules.py` + `config/strategy.yaml`。

**测试命令统一:** `PYTHONPATH=src python3 -m pytest <path> -q`(repo 约定,见各测试文件头)。

---

## File Structure

| 文件 | 责任 | 改动 |
|---|---|---|
| `src/sca/strategy_rules.py` | 纯定价规则(单源) | **+** `rung_for(rungs,i)` clamp helper |
| `src/sca/live/order_recon.py` | 纯 desired-order 计算(live maker) | `rungs[i]` → `rung_for(rungs,i)`(`:123`) |
| `src/sca/live/engine.py` | 引擎:R1 gate + 启动序列 + 状态/sim 定价 | **+** `_topup_to_cap`;wire 进 `_reconcile_or_refuse`;`self.rungs[i]` → `rung_for(...)`(`:746`,`:909`) |
| `config/strategy.yaml` | 单源配置 | USDC `max_total_alloc_usd` 400→1000(数据) |
| `tests/test_rung_for_clamp.py` | 新:rung 越界 | rung_for + desired_orders 2-slice |
| `tests/test_engine_topup.py` | 新:top-up 单元 + 集成 | headroom/幂等/cap-bound/成本保留/wiring |

---

## Task 1: `rung_for` clamp — 修三处 rung 越界(基础,先做)

**理由先行**:top-up 后 USDC(config `rungs:[1]`)会有 2 个 slice。slice index 1 翻成卖态时 `rungs[1]` IndexError。真崩点在 live maker `order_recon.py:123`(`desired_orders`),另两处 `engine.py:746`(dryrun sim)、`:909`(status)。统一 clamp:溢出 slice 复用最后一档;单档 USDC = 所有 slice 用 `rungs[0]`(老板选的"两腿各单档");对 USD1 N5(slice≤rungs)**零行为变化**。

**Files:**
- Modify: `src/sca/strategy_rules.py`(新增 `rung_for`)
- Modify: `src/sca/live/order_recon.py:123`
- Modify: `src/sca/live/engine.py:746`,`:909`
- Test: `tests/test_rung_for_clamp.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_rung_for_clamp.py
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from sca.strategy_rules import rung_for          # noqa: E402
from sca.live.order_recon import desired_orders   # noqa: E402

def test_rung_for_within_bounds():
    assert rung_for([1, 2, 3], 0) == 1
    assert rung_for([1, 2, 3], 2) == 3

def test_rung_for_clamps_overflow_to_last():
    assert rung_for([1], 1) == 1        # single-rung USDC: slice 1 reuses rung 0
    assert rung_for([1], 5) == 1
    assert rung_for([1, 2], 4) == 2     # multi-rung: clamp to last

def test_desired_orders_two_usd1_slices_single_rung_no_indexerror():
    # 2 USDC-holding (sell) slices but rungs=[1] -> must NOT raise; both rest a SELL.
    slices = [
        {"state": "usd1", "qty": 400.0, "cash": 0.0, "sell_px": 0.0, "entry": 0.9998,
         "order_link_id": None},
        {"state": "usd1", "qty": 600.0, "cash": 0.0, "sell_px": 0.0, "entry": 0.9999,
         "order_link_id": None},
    ]
    out = desired_orders(1.0, slices, [1], rebuy_off_bp=-1.0, tick=1e-4, lot=1e-6,
                         avail_base=1000.0, avail_quote=0.0, min_qty=0.0, min_cost=0.0)
    assert set(out.keys()) == {0, 1}                 # both slices emitted a desired order
    assert all(d.side == "Sell" for d in out.values())
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `PYTHONPATH=src python3 -m pytest tests/test_rung_for_clamp.py -q`
Expected: FAIL — `ImportError: cannot import name 'rung_for'`(rung_for 未定义)。

- [ ] **Step 3: 加 `rung_for` 到 strategy_rules.py**

在 `final_sell_price` 之后新增:
```python
def rung_for(rungs, i: int) -> float:
    """The sell rung (bp) for slice index ``i``, clamped to the LAST configured rung when
    slices outnumber rungs (a single-rung ladder topped up past 1 slice). For i < len(rungs)
    this is exactly rungs[i] (zero change for USD1 N5, slices <= rungs). Single-rung USDC =>
    every slice uses rungs[0]."""
    return rungs[i] if i < len(rungs) else rungs[-1]
```

- [ ] **Step 4: 应用到 order_recon.py(live maker 真崩点)**

`order_recon.py` 顶部 import 加 `rung_for`(与既有 `from sca.strategy_rules import ...` 合并),并改 `:123`:
```python
# was: px = final_sell_price(anchor, rungs[i], s.get("entry"),
px = final_sell_price(anchor, rung_for(rungs, i), s.get("entry"),
                      min_profit_bp, rest_bps, tick,
                      sell_round=sell_round, min_sell_margin_bp=min_sell_margin_bp)
```

- [ ] **Step 5: 应用到 engine.py(dryrun sim `:746` + status `:909`)**

engine.py 顶部 import 加 `rung_for`(与 `:106` 既有 `from sca.strategy_rules import (...)` 合并)。
- `:746`(evaluate_fills): `final_sell_price(a, self.rungs[i], ...)` → `final_sell_price(a, rung_for(self.rungs, i), ...)`
- `:909`(status sl_out): `self._status_sell_price(a, self.rungs[i], s.get("entry"))` → `self._status_sell_price(a, rung_for(self.rungs, i), s.get("entry"))`

> `:888` 的 `zip(self.fracs, self.rungs)` 不动 —— 它是 config-rung 驱动的 ladder 指标(`sell_rungs`),非 per-slice;单档配置显示 1 个 rung 行是对的。

- [ ] **Step 6: 跑测试确认 GREEN + 回归**

Run: `PYTHONPATH=src python3 -m pytest tests/test_rung_for_clamp.py tests/test_engine_maker_runloop.py tests/test_engine_maker_fills.py -q`
Expected: PASS(新测试绿 + maker 路径回归绿)。

- [ ] **Step 7: Commit**

```bash
git add src/sca/strategy_rules.py src/sca/live/order_recon.py src/sca/live/engine.py tests/test_rung_for_clamp.py
git commit -m "fix(live): rung_for clamp — slice 数 > 配置 rung 数时不再 IndexError(top-up 前置)"
```

---

## Task 2: `_topup_to_cap` 方法(headroom 算法,单元可测)

**Files:**
- Modify: `src/sca/live/engine.py`(在 `_seed_slices_from_balance` 之后新增 `_topup_to_cap`)
- Test: `tests/test_engine_topup.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_engine_topup.py
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from sca.live.engine import PaperEngine          # noqa: E402

def _bal_uc(usdc=0.0, usdt=0.0):
    total = usdc + usdt
    return {"account_type": "spot",
            "totals": {"equity_usd": total, "wallet_usd": total, "available_usd": total,
                       "im_usd": 0.0, "mm_usd": 0.0, "perp_upl_usd": 0.0},
            "coins": {"USDC": {"wallet": usdc, "locked": 0.0, "free": usdc, "usd": usdc, "borrow": 0.0},
                      "USDT": {"wallet": usdt, "locked": 0.0, "free": usdt, "usd": usdt, "borrow": 0.0}}}

def _eng(tmp_path, slices, cap=1000.0):
    eng = PaperEngine(symbol="USDCUSDT", mode="paper", seconds=1, csv_path=str(tmp_path / "o.csv"))
    eng.armed = True
    eng.maker_enabled = True
    eng.slices = [dict(s) for s in slices]
    eng.deployed = bool(slices)
    eng._resumed = True
    eng._max_total_alloc_usd = cap
    eng._deployed_capital = sum(s["qty"] for s in slices) + sum(s.get("cash", 0.0) for s in slices)
    return eng

def _usdc_slice(qty, entry=0.9998):
    return {"state": "usd1", "qty": qty, "cash": 0.0, "sell_px": 0.0, "entry": entry}

def test_topup_deploys_headroom(tmp_path):
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0)
    eng._topup_to_cap(_bal_uc(usdc=400.0, usdt=600.0), [])
    assert len(eng.slices) == 2
    new = eng.slices[1]
    assert new["state"] == "usdt" and new["entry"] is None
    assert abs(new["cash"] - 600.0) < 1e-6          # headroom = 1000 - 400
    assert eng.slices[0] == _usdc_slice(400.0)      # existing slice (cost) UNTOUCHED

def test_topup_idempotent(tmp_path):
    eng = _eng(tmp_path, [_usdc_slice(400.0), {"state": "usdt", "qty": 0.0, "cash": 600.0,
                                               "sell_px": 0.0, "entry": None}], cap=1000.0)
    eng._topup_to_cap(_bal_uc(usdc=400.0, usdt=600.0), [])
    assert len(eng.slices) == 2                      # already at cap -> no-op

def test_topup_cap_bound_ignores_excess_idle(tmp_path):
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0)
    eng._topup_to_cap(_bal_uc(usdc=400.0, usdt=5000.0), [])   # overfunded
    assert abs(eng.slices[1]["cash"] - 600.0) < 1e-6          # only headroom, NOT 5000

def test_topup_no_idle_quote_noop(tmp_path):
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0)
    eng._topup_to_cap(_bal_uc(usdc=400.0, usdt=0.0), [])
    assert len(eng.slices) == 1

def test_topup_cap_negative_noop(tmp_path):
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=-1.0)
    eng._topup_to_cap(_bal_uc(usdc=400.0, usdt=600.0), [])
    assert len(eng.slices) == 1

def test_topup_accumulates_deployed_capital(tmp_path):
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0)
    base = eng._deployed_capital
    eng._topup_to_cap(_bal_uc(usdc=400.0, usdt=600.0), [])
    assert abs(eng._deployed_capital - (base + 600.0)) < 1e-6
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `PYTHONPATH=src python3 -m pytest tests/test_engine_topup.py -q`
Expected: FAIL — `AttributeError: 'PaperEngine' object has no attribute '_topup_to_cap'`。

- [ ] **Step 3: 实现 `_topup_to_cap`**

在 engine.py `_seed_slices_from_balance` 方法之后新增:
```python
def _topup_to_cap(self, bal: dict, open_orders) -> None:
    """Resumed-deployed restart: deploy idle quote (USDT) up to the (possibly raised) cap,
    preserving existing slices and their cost-basis ``entry`` UNTOUCHED. Appends ONE quote-
    state slice for headroom = cap - current MTM slice value, bounded by the REAL idle quote
    in the wallet (so it can never deploy phantom funds). Idempotent: once slice value ~ cap,
    headroom <= tol -> no-op. Runs INSIDE the R1 gate BEFORE reconcile() decides, so the
    topped-up summary is reconciled against real balance (defense-in-depth)."""
    cap = self._max_total_alloc_usd
    if cap < 0:
        return                                       # -1 = whole-wallet: top-up unsupported (P2)
    tol = float(_LIVE.get("reconcile_tol", 1.0))
    base_coin, quote_coin = self._coins()
    wal_base = self._wallet_coin(bal, base_coin)
    wal_quote = self._wallet_coin(bal, quote_coin)
    base_mark = (self._coin_usd(bal, base_coin) / wal_base) if wal_base > 0 else 1.0
    quote_mark = (self._coin_usd(bal, quote_coin) / wal_quote) if wal_quote > 0 else 1.0
    slice_value = sum(s["qty"] * base_mark + s.get("cash", 0.0) * quote_mark
                      for s in self.slices)
    headroom = cap - slice_value
    if headroom <= tol:
        return                                       # at/over cap -> no-op (idempotent core)
    idle_quote = wal_quote - sum(s.get("cash", 0.0) for s in self.slices)
    deploy = min(idle_quote, headroom / quote_mark if quote_mark > 0 else headroom)
    if deploy <= tol:
        return                                       # no real idle quote to deploy
    s = {"state": "usdt", "qty": 0.0, "cash": deploy, "sell_px": 0.0, "entry": None}
    s.update(dict(_ORDER_FIELD_DEFAULTS))
    self.slices.append(s)
    self._deployed_capital = getattr(self, "_deployed_capital", 0.0) + deploy * quote_mark
```

- [ ] **Step 4: 跑测试确认 GREEN**

Run: `PYTHONPATH=src python3 -m pytest tests/test_engine_topup.py -q`
Expected: PASS(6 tests)。

- [ ] **Step 5: Commit**

```bash
git add src/sca/live/engine.py tests/test_engine_topup.py
git commit -m "feat(live): _topup_to_cap — headroom 部署闲置 USDT 到 cap, 保留旧仓成本(单元)"
```

---

## Task 3: wire `_topup_to_cap` 进 R1 gate(启动序列集成)

**Files:**
- Modify: `src/sca/live/engine.py`(`_reconcile_or_refuse`,`:1227` seed 分支旁加 elif)
- Test: `tests/test_engine_topup.py`(追加集成测试)

- [ ] **Step 1: 写失败集成测试(追加)**

```python
# 追加到 tests/test_engine_topup.py
class _FakeClient:
    def __init__(self, bal, orders=None):
        self._b, self._o = bal, orders or []
    def get_wallet_balance(self): return self._b
    def get_open_orders(self, symbol=None): return self._o

def test_gate_tops_up_then_proceeds(tmp_path):
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0)
    eng.persist = True
    eng.allow_fresh = False
    rep = eng._reconcile_or_refuse(client=_FakeClient(_bal_uc(usdc=400.0, usdt=600.0), []))
    assert rep["action"] == "proceed"
    assert len(eng.slices) == 2                       # topped up inside the gate
    assert abs(eng.slices[1]["cash"] - 600.0) < 1e-6

def test_gate_no_idle_resumes_unchanged(tmp_path):
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0)
    eng.persist = True; eng.allow_fresh = False
    rep = eng._reconcile_or_refuse(client=_FakeClient(_bal_uc(usdc=400.0, usdt=0.0), []))
    assert rep["action"] == "proceed"
    assert len(eng.slices) == 1                       # nothing idle -> no top-up
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `PYTHONPATH=src python3 -m pytest tests/test_engine_topup.py::test_gate_tops_up_then_proceeds -q`
Expected: FAIL — `len(eng.slices) == 1`(gate 尚未调 topup,slices 不增)。

- [ ] **Step 3: 接线**

engine.py `_reconcile_or_refuse` 中,现有 seed 分支(`:1227`):
```python
# was:
if self.maker_enabled and not self.slices:
    self._seed_slices_from_balance(bal, open_orders)
# becomes:
if self.maker_enabled and not self.slices:
    self._seed_slices_from_balance(bal, open_orders)
elif self.maker_enabled and self.deployed and self.slices:
    self._topup_to_cap(bal, open_orders)            # resumed-deployed -> fill cap headroom
```

- [ ] **Step 4: 修 status 行分母(persona 自审 P2 — canary 可读性)**

top-up 后 `len(self.slices) > self.n`(self.n=配置 rung 数)。status 日志行 `:1035`
`f"| usd1={pos['n_in_usd1']}/{self.n} "` 分母会 stale(显示 /1 而非实际 /2),canary 时误导。改用 live slice 数:
```python
# engine.py:1035  was: ...usd1={pos['n_in_usd1']}/{self.n} ...
#                 now: ...usd1={pos['n_in_usd1']}/{len(self.slices)} ...
```
> 仅改这一处 recurring status 行;`:683` 一次性 bootstrap 日志的 `{self.n} slices`(="N 配置档")保留不动。纯日志串,无下游 parser,零风险。

- [ ] **Step 5: 跑测试确认 GREEN + gate 回归**

Run: `PYTHONPATH=src python3 -m pytest tests/test_engine_topup.py tests/test_engine_recon_gate.py tests/test_engine_resume.py -q`
Expected: PASS(新集成测试 + gate/resume 回归全绿)。

- [ ] **Step 6: Commit**

```bash
git add src/sca/live/engine.py tests/test_engine_topup.py
git commit -m "feat(live): wire _topup_to_cap 进 R1 gate(reconcile 前, resumed-deployed)+ status 分母用 live slice 数"
```

---

## Task 4: config cap 400→1000 + 全量回归

**Files:**
- Modify: `config/strategy.yaml`(USDC `max_total_alloc_usd`)

- [ ] **Step 1: 改配置(数据)**

`config/strategy.yaml` `universe` 下 `USDCUSDT`:
```yaml
    max_total_alloc_usd: 1000   # was 400 — top-up 放大目标(部署机已同步)
```

- [ ] **Step 2: 全量测试**

Run: `PYTHONPATH=src python3 -m pytest -q`
Expected: PASS,全绿(≈561 baseline + 本轮新增)。**重点确认 USD1@Bybit 路径零变化**(rung_for 对 slice≤rungs 无影响;USD1 backtest pin 不变)。

- [ ] **Step 3: USD1 回归数值 pin(防 rung clamp 误伤多档)**

Run: `PYTHONPATH=src python3 -m pytest tests/test_engine_maker_runloop.py tests/test_reconcile.py tests/test_maker_persistence_resume.py -q`
Expected: PASS。若有 USD1 backtest 数值 pin 测试(grep `2.661` / `desired_orders`),确认未漂移。

- [ ] **Step 4: Commit**

```bash
git add config/strategy.yaml
git commit -m "config(live): USDC max_total_alloc_usd 400->1000(top-up 放大目标)"
```

---

## Self-Review(写完计划自查)

**1. Spec coverage**:
- §2.1 启动序列接线 → Task 3 ✓
- §2.2 headroom 算法 → Task 2 ✓
- §2.3 不变量(cap-bound/幂等/成本保留/真金有据/单档)→ Task 2 测试覆盖五项 ✓
- §3 rung 越界(三处)→ Task 1 ✓(含 live maker `order_recon.py:123`)
- §4 改动文件 → Task 1-4 全覆盖 ✓
- §5 测试清单 → Task 1-3 测试映射 ✓
- §6 config cap → Task 4 ✓

**2. Placeholder scan**:无 TBD/TODO;每个 code step 含完整代码与命令。`cap<0` 是显式 scope-out(P2,测试 no-op)。

**3. Type/signature consistency**:`rung_for(rungs, i)` 在 Task1 定义、Task1 三处调用签名一致;`_topup_to_cap(self, bal, open_orders)` Task2 定义、Task3 调用一致;测试 helper `_eng/_bal_uc/_usdc_slice` 全文件一致。

**4. 风险点回归**:Task1 Step6 + Task4 Step2/3 三道回归守 USD1@Bybit 零变化。
