# USDC 实盘 Top-up(放大到 cap)— 设计 Spec

> 状态:待老板审 · 日期 2026-06-29 · 分支 `worktree-dev-usdc-live-topup`

## 1. 目标与决策

**目标**:实盘 maker 引擎在**重启**时,保留现有已部署仓位(按原成本 `entry` 继续挂卖),
并用 `headroom = max_total_alloc_usd − 现有持仓估值` 的金额,把钱包里**闲置的 quote(USDT)**
部署成新的买单,从而把单 symbol 放大到新的 `max_total_alloc_usd`,而**不丢失旧仓的成本基准**。

**触发场景(真实)**:USDC@Bitget 实盘从 $400 cap 提到 $1000。当前内置路径做不到——
`_maybe_deploy` 仅在 `not self.deployed` 时部署(`engine.py:694`),已部署的 resumed bot 永不加仓;
唯一放大手段是删 state 重 seed(`_seed_slices_from_balance`),但 reseed 把 `entry` 重设为**当前市价 mark**
(`engine.py:1366`),丢掉旧 USDC 的原买入成本 → 卖单不再锁原价差。

**老板决策(2026-06-29)**:
1. **档位结构 = 单档**:旧 USDC 1 档按原成本卖 + 新 USDT 1 档 @anchor-1bp 买,两腿各单档
   (符合项目"挂 1bp 单档最优"回测结论,挂 2bp 频率掉 25%)。
2. **触发 = always-auto**:不加 config flag;只要 resumed-deployed 且 `headroom>tol` 且有闲置 USDT,
   重启即自动补到 cap。安全由**机制**兜(cap-bounded + reconcile 前置校验),非开关。

**老板知情自担**:USDC `apr: 0.0`(零 carry),项目诚实结论是价差 edge OOS≈0、yield 才是引擎
(`docs/FINDINGS.md`)。放大 USDC = 放大一个零息、样本外≈0 边际的纯交易盘。这是"该不该做"的取舍,老板已拍。

## 2. 架构 / 数据流

### 2.1 启动序列接线(关键安全点)

新方法 `_topup_to_cap(bal, open_orders)`,挂在 `_reconcile_or_refuse`(`engine.py:1200`)内,
**fetch 余额之后 / 调 `reconcile()`(`:1244`)之前**,与现有 seed 分支(`:1227`)并列:

```
现有:   if self.maker_enabled and not self.slices:        # 空状态 → fresh seed
            self._seed_slices_from_balance(bal, open_orders)
新增:   elif self.maker_enabled and self.deployed and self.slices:   # resumed-deployed → top-up
            self._topup_to_cap(bal, open_orders)
        ... reconcile(self._local_summary(), bal, open_orders, ...)  # 校验含新增 slice 的全量状态
```

**两层安全(互补)**:
- **主安全 = idle 从真实余额算**:`idle_quote` 来自本次 fetch 的真实钱包余额(§2.2),`deploy ≤ idle_quote`。
  → top-up **只可能部署钱包里真实闲置的 USDT**,绝不会凭空挂买单。老板若没充那 $600 → `idle_quote≤tol` → **early-return no-op**(不是 refuse,是干脆不补)。
- **Defense-in-depth = reconcile 前置**:append 后 `_local_summary()`(`:1099`)的 `quote_qty=Σcash` 含新增,
  `reconcile()` 以 lower-bound(`dedicated_account=false`)校验 `wallet_USDT ≥ Σcash`。由构造恒满足
  (`deploy ≤ idle = wallet−旧Σcash` → 新Σcash ≤ wallet),但若日后算术漂移,这层会 refuse 兜住。
  R1 守住整个 topped-up 状态,top-up 不绕过对账。

### 2.2 `_topup_to_cap` 算法(全程 coin-amount,cap 用 mark 折 USD)

```
base_coin, quote_coin = self._coins()                  # (USDC, USDT)
cap = self._max_total_alloc_usd                         # 新 cap(=1000)
if cap < 0: return                                      # -1=全钱包语义,本期不 top-up(§5 测试 no-op)

base_mark  = _coin_usd(bal,base)/wallet_base  (>0; $1 fallback)   # 现有 base 腿市价
quote_mark = _coin_usd(bal,quote)/wallet_quote (>0; $1 fallback)  # ≈1
slice_value_usd = Σ(s.qty × base_mark) + Σ(s.cash × quote_mark)   # 现有持仓 MTM 估值
headroom_usd    = max(0.0, cap − slice_value_usd)
if headroom_usd <= tol: return                          # 已满 → no-op(幂等核心)

idle_quote = wallet_quote − Σ(s.cash)                   # 真实钱包里未被现有 slice 占用的 USDT
deploy_amt = min(idle_quote, headroom_usd / quote_mark) # 同时受"真实闲钱"与"cap headroom"封顶
if deploy_amt <= tol: return                            # 无可补

→ append 1 个 slice:{state:"usdt", qty:0.0, cash:deploy_amt, sell_px:0.0, entry:None}
                     + _ORDER_FIELD_DEFAULTS
self._deployed_capital += deploy_amt × quote_mark       # PnL 基线诚实累加(face=USDT)
self.deployed = True ; self._resumed = True             # 已是 True(resumed-deployed),幂等重申
```

`deploy_amt = min(idle_quote, headroom_usd/quote_mark)` 一行同时保证 **真实闲钱上限** 与 **cap 上限**
(headroom ≤ cap),无需再单独调 `_deployable_amt`。

**不修改任何现有 slice** → 旧 USDC slice 的 `entry`=原成本原样保留(resume 从 JSON 加载,`engine.py:579`),
run loop 继续按 `final_sell_price(anchor, rung, 原entry, ...)`(`strategy_rules.py:90`)挂卖,锁原价差。

### 2.3 不变量

| 不变量 | 保证方式 |
|---|---|
| **cap-bounded** | `deploy ≤ headroom = cap − slice_value`;wallet 超出 cap 的闲钱绝不动 |
| **幂等** | 补完 slice_value≈cap → 下次重启 headroom=0 → no-op |
| **成本保留** | 只 append、不碰现有 slice;旧 `entry` 不变 |
| **真金有据** | `idle_quote` 由真实 fetch 余额算,`deploy≤idle` → 只部署真实闲钱;reconcile 前置为 defense-in-depth |
| **单档** | 新 slice 与旧 slice 都用 `rungs[0]=1bp`(见 §3) |

## 3. 必须修的坑:rung 越界(否则 2 slice 直接崩)

USDC config `rungs:[1]`(长度 1)。top-up 后出现第 2 个 slice,卖价/状态路径按 `self.rungs[i]` 索引
(`engine.py:746`、`:909`,及 `_status_sell_price` 调用点)→ `self.rungs[1]` **IndexError**。

**修法**:加 clamp helper
```python
def _rung_for(self, i: int) -> float:
    r = self.rungs
    return r[i] if i < len(r) else r[-1]    # 单档 USDC → 所有 slice 用 rungs[0]=1bp
```
替换全部 `self.rungs[i]` 索引点。语义:slice 数超过配置 rung 数时,溢出 slice 复用最后一档;
对单档 USDC = 所有 slice 同 1bp(正是老板选的"两腿各单档")。对多档 symbol(USD1 N5,slice≤5)零行为变化。

## 4. 改动文件

| 文件 | 改动 |
|---|---|
| `src/sca/live/engine.py` | 新增 `_topup_to_cap` + `_rung_for`;`_reconcile_or_refuse` 接线(elif 分支);替换 `self.rungs[i]` 索引点 |
| `config/strategy.yaml` | `universe[USDCUSDT].max_total_alloc_usd` 400→1000(数据,对齐部署机) |
| `tests/` | top-up 单元 + 启动序列集成 + rung 越界 + 回归 |

**回滚**:删 `_reconcile_or_refuse` 里那一行 `elif ... _topup_to_cap()` 调用 → 立即回到纯 resume
(旧仓继续按原 cap 跑);`_rung_for` 对既有单/多档 symbol 行为不变,可留。

## 5. 测试(TDD 覆盖)

- **headroom 算法**:MTM 估值、cap-bound(deploy≤headroom)、idle 扣除现有 slice cash
- **幂等**:补满后二次重启 `_topup_to_cap` no-op(slice 数不增、cash 不变)
- **reconcile 前置契约**:钱不够(wallet_USDT<Σcash)→ refuse;够 → proceed
- **成本保留**:append 后现有 USDC slice 的 `entry` 与 qty 不被动(逐字段 pin)
- **rung 越界安全**:2 slice + config rungs=[1] → slice[1] 卖价用 rungs[0],不抛 IndexError
- **新 slice 生命周期**:USDT slice fill → flip 成 usdc-state → 按单档 rungs[0] 挂卖
- **cap<0(-1)**:不 top-up(语义留待后续,本期不支持全钱包 top-up)
- **回归**:USD1@Bybit N5 路径零变化(rung clamp 对 slice≤rungs 无影响);全量测试绿

## 6. 部署前置 + 运维(canary)

1. repo `config/strategy.yaml` USDC cap 400→1000(本 spec 内含);部署机 pull 后验 commit。
2. 重启前给 **Bitget USDC 子账户**充 **≥$600 闲置 USDT**,且整体单边干净
   (除 USDC 持仓腿外,quote 只有这笔 USDT,避免 reconcile 混合歧义)。
3. 重启 `bot-usdc` → 期望日志:resume 旧 USDC slice → `_topup_to_cap` append USDT slice →
   `R1 reconciliation OK -> proceed` → 旧 USDC 按**原成本**挂卖 + 新 USDT 按 headroom @anchor-1bp 挂买,
   总部署≈$1000。
4. **always-auto 语义提醒**:此后只要该子账户有闲置 USDT 且 slice_value<cap,任何重启都会补到 cap;
   勿往此子账户放"不想被部署"的 USDT。

## 7. 风险

- **真金 + R1 安全路径**:靠 reconcile 前置校验 + cap-bound + 完整 TDD + Codex 异构审 + canary 兜。
- **always-auto 无开关**:机制兜底(cap-bounded + 钱不在即 refuse),但语义上任何重启会动用闲置 USDT 到 cap——已在 §6.4 标注。
- **ROI**:USDC 零息、edge OOS≈0,放大不增期望边际(老板知情)。
- **回归红线**:USD1@Bybit 路径零变化,merge 前全量测试绿。
