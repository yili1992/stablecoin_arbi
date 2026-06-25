# Per-Symbol Strategy 参数 Override — 设计 Spec

> 状态：待老板审 · 日期 2026-06-25 · 分支 `worktree-persymbol-override`

## 1. 背景与目标

当前所有 symbol 共用一套**全局** strategy 参数（`rungs`/`fractions`/`min_profit_bp`/`rest_bps`/
`anchor_ema_span`/`rebuy_offset_bp`），且是 **import-time 模块常量**
（`engine.py:105-114`、`backtest/strategy.py:88-93`）。

目标：支持 **per-symbol 参数差异化**，让 USD1 与 USDC 各用最适合自己的配置：
- **USD1**：默认 N5 `[1,2,3,4,5]`（有 8% carry → 保息为主，行为**零变化**）
- **USDC**：override 为 **N1** `rungs=[1]`/`fractions=[1.0]`、`interest_apr=0`（无息 → 纯交易最大化）

## 2. 已确认决策（老板）

| 项 | 决策 |
|---|---|
| 覆盖粒度 | **全部 strategy 参数**可 per-symbol override（dict-merge） |
| USDC 配置 | `rungs=[1]`、`fractions=[1.0]`、`interest_apr=0` |
| USD1 配置 | **无 override** → 用默认 strategy 块（N5），行为零变化 |
| 落地 | 上 **live 真钱** |
| 资金 | 两个 symbol **各自独立子账户**，各 `max_total_alloc_usd=$1000` |
| 运行架构 | 沿用单 symbol engine，USD1/USDC **各跑一个实例** |

## 3. 设计依据（回测，adv=0·touch·排除生息）

- USDC N1 单币 OOS **6.05%**（4 段 sub-period 全胜，单币最优）；USD1 N5 **含息** 9.34%（carry 主导）。
- 差异化逻辑：**有 carry 的 USD1 保 N5 护确定收益；无 carry 的 USDC 用 N1 博交易 edge**。
- 共享池/条件单复用已证伪（1.30% < 各半 3.12% < 全押单币 3.66%；复用 ≤ 全押最优单币）→ **不采用**，改各自独立。

## 4. 架构

### 4.1 数据流
```
config/strategy.yaml
  strategy:            (默认参数, USD1 用)
  universe[USDCUSDT].strategy:   (override)
        │
        ▼
config.strategy_for(symbol)   ← 新增 resolver (沿用 runtime()/strategy() 模式)
  = merge(默认 strategy 块, universe[symbol].strategy override) + 默认值填充
        │
        ├──► backtest(symbol=…)  按 symbol 解析
        └──► engine.__init__(symbol)  按 symbol 解析 → self.*
```

### 4.2 组件改动（5 文件 + 测试）

**① `src/sca/config.py` — 新增 resolver（核心）**
```python
_STRATEGY_PARAM_DEFAULTS = {
    "rungs": [5,7,10,14,20], "fractions": [0.15,0.18,0.20,0.22,0.25],
    "min_profit_bp": 0.0, "rest_bps": 0.0, "anchor_ema_span": 21,
    "rebuy_offset_bp": -1.0, "interest_apr": 0.10,
}
def strategy_for(symbol: str, cfg: dict | None = None) -> dict:
    """该 symbol 的有效 strategy 参数 = 默认 strategy 块 ← universe[symbol].strategy override。
    无 override 的 symbol 原样返回默认（USD1 行为零变化）。cfg 可注入（测试）。"""
    c = CFG if cfg is None else cfg
    base = dict(c.get("strategy", {}))
    for u in c.get("universe", []):
        if u.get("symbol") == symbol:
            base.update(u.get("strategy", {}) or {})
            break
    out = {k: base.get(k, d) for k, d in _STRATEGY_PARAM_DEFAULTS.items()}
    # 不变量：fractions 和 ≈1、与 rungs 等长（fail-fast，防 live 误配）
    assert abs(sum(out["fractions"]) - 1.0) < 1e-9, f"{symbol} fractions 和≠1"
    assert len(out["rungs"]) == len(out["fractions"]), f"{symbol} rungs/fractions 长度不一致"
    return out
```

**② `config/strategy.yaml` — USDC 进 universe + override**
```yaml
universe:
  - symbol: USD1USDT
    apr: &usd1_apr 0.08
    kind: reserve
    # 无 strategy override → 用默认 strategy 块 (N5)
  - symbol: USDCUSDT
    apr: 0.0              # USDC 持有 0 息
    kind: reserve
    strategy:            # per-symbol override (N1, 纯交易)
      rungs: [1]
      fractions: [1.0]
      interest_apr: 0.0
  - symbol: USDEUSDT  …  (不变)
# 默认 strategy 块、max_total_alloc_usd=1000 均不变
```

**③ `src/sca/backtest/strategy.py`**
- `backtest(adv, *, symbol=None, params=None, with_yield=True, fill_mode="touch", …)`
- 参数解析**三层优先级**（向后兼容最强）：
  1. `params` 给定 → 用 `params`（sweep / 实验脚本显式传参）
  2. 否则 `symbol` 给定 → 用 `strategy_for(symbol)`
  3. 否则（都 None）→ 用**模块全局**（现状 `RUNG_BP/FRACS/…`，无参调用零变化）
- 用解析出的局部 `sp[...]` 替换函数体内对模块全局的引用
- 回归保证：`backtest()` 无参 == 改造前；且测试 pin `strategy_for("USD1USDT") == 模块全局默认值`（两条路径数值必须一致）

**④ `src/sca/live/engine.py`**
- `__init__(symbol)`：`sp = strategy_for(symbol)` → 设 `self.rungs/self.fracs/self.min_profit_bp/self.rest_bps/self.anchor_ema_span/self.rebuy_off_bp/self.interest_apr`
- 把残余直接读全局的 4 处改 `self.*`：`ANCHOR_EMA_SPAN`(L331/658/938)、`REBUY_OFF_BP`(L734/839-840/1630)、`APR`(L 利息累计处)
- `order_recon.desired_orders(...)` 已接受 `rungs` 参数 → 传 `self.rungs`，**无需改 order_recon**

**⑤ `src/sca/optimize/sweep.py`** — 改传 `backtest(params=…)` 而非 monkeypatch 全局 `S.FRACS/S.RUNG_BP`

**不需要改**：`order_recon.py`（已参数化）、`dashboard.py`（从 status JSON 读）、`strategy_rules.py`（纯函数）

## 5. 测试计划（TDD）

| 测试 | 断言 |
|---|---|
| `test_config_strategy_for` | 默认无 override=默认；USDC override 后 rungs=[1]/fractions=[1.0]/apr=0；merge 只覆盖给定键；不变量 assert 触发 |
| **回归（关键）** | `strategy_for("USD1USDT")` == 改造前的全局默认值（bit-parity）；USD1 `backtest()` 改造前后结果**完全一致** |
| `test_engine` per-symbol | USDC engine `self.rungs==[1]`；USD1 engine `self.rungs==[1,2,3,4,5]`；engine state 文件格式不变 |
| `backtest(symbol="USDCUSDT")` | 复现 N1 OOS≈6% |
| 全量回归 | 现有 22 个测试文件全绿（尤其 `test_smoke`/`test_engine_*`/`test_strategy_*`） |

## 6. 风险 / 回归保证 / 回退

- **USD1 零变化（硬保证）**：USD1 不写 override → `strategy_for` 返回默认 = 现状 N5。回归测试 pin 死 backtest 结果 + engine `self.*` 值 + state 文件格式 → **不破坏现有 USD1 live state**。
- **N1 live 风险**：1bp 单档 = touch 最高估、队列损耗最重；`$1000` cap 限损，canary 实测真实成交率/markout（这正是上 live 的目的，dryrun 模拟撮合测不到真实排队）。
- **回退**：删 USDC 的 universe 条目即完全恢复，无残留。
- **运维前置**（老板）：配置 USDC 独立子账户 API key + 充值 $1000；USD1 子账户照旧。

## 7. 落地范围

机制（全参数 override）+ USDC N1 配置 + USD1 默认 + 各 $1000 独立子账户 → 上 live 双实例。
**不含**：condition-order/共享池（已证伪）、engine 多 symbol 单进程改造（YAGNI）。
