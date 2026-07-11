# Plan — 斩仓统一卖价(surrender unified sell price)

状态:待审批(worktree `dev-surrender-unified-price`,基底 a7ce99b)。已过 persona 自审 + Codex 异构审查(见 §9)。

## 1. 目标

USD1 触发斩仓(`rest_bps` 击穿:anchor EMA 跌破入场成本 `rest_bps` bp)时,**触发斩仓的 slice 用统一卖价**
`round_to_tick(anchor + surrender_rung_bp·bp, tick, sell_round)`(新增可配参数,默认启用值 `1` bp)。USD1 `sell_round: floor` → 即 `floor(anchor+1bp)`。不再各 slice 各自 `rung_i`(1/2/3/4/5 bp)。

- 保持 5 个 slice **独立挂单、同价**(不物理合并订单 —— 老板 2026-07-11 定:5 单同价与物理 1 单成交/PnL 撮合等价,仅订单列表视觉差别,取轻量方案)。
- 改动**下沉到单源 `sell_price_raw`**,自动保证 backtest == live == paper == dashboard 四口径一致(项目红线:卖价单源不漂移)。

## 2. 背景 / 根因

- **reset = `rest_bps` surrender 机制**(config `rest_bps: 14`):`surrender_sell(anchor, entry, rest_bps)` 为真时,`final_sell_price` 豁免 `min_sell_margin_bp` 与 `min_profit_bp` floor,以 `anchor + rung` 认亏卖出「重置成本」。
- **现状缺陷**:斩仓分支 `base` 保持 `anchor`,但 **rung 仍是外部按 slice 传入的 `rung_for(rungs, i)`** → 5 个 slice 斩仓卖价 = `anchor+1bp…anchor+5bp` 分散 5 个价。高档 slice 挂太高,斩仓时反而拖着不走,违背「快速一口价清仓」本意。
- `final_sell_price` 生产调用恰 4 处:回测 `backtest/strategy.py:208`、live 挂单 `live/order_recon.py:127`、live 成交 `live/engine.py:769`、status 展示 `live/engine.py:899`。

## 3. 设计

### 3.1 核心:`sell_price_raw` 下沉(strategy_rules.py)

新增参数 `surrender_rung_bp: float | None = None`。语义:**斩仓时用它替换传入的 `rung_bp`**;`None` = 用原 `rung`(旧行为)。

```python
def sell_price_raw(anchor, rung_bp, entry=None, min_profit_bp=0.0, rest_bps=0.0,
                   surrender_rung_bp=None):
    a = float(anchor); rung = float(rung_bp); min_profit = float(min_profit_bp)
    e = _finite(entry)
    if surrender_sell(a, e, rest_bps):                 # 斩仓分支(提前 return)
        eff_rung = rung if surrender_rung_bp is None else float(surrender_rung_bp)
        return a + eff_rung * BP                        # base=anchor(不依赖 entry/i)→ raw 与 slice 无关
    base = a
    if min_profit > 0 and e is not None:                # 非斩仓:利润 floor(与原逻辑等价)
        base = max(a, e * (1 + min_profit * BP))
    return base + rung * BP
```

- **统一价来源**:斩仓分支返回的 raw = `anchor + eff_rung·bp`,`base` 不依赖 `entry`、`eff_rung` 不依赖 slice `i`。`final_sell_price` 对该 raw 做 `round_to_tick(raw, tick, sell_round)` —— 同 `anchor`/同 `surrender_rung_bp`/同 `sell_round` ⇒ **所有斩仓 slice 得同一价**(统一性由 sell_round 口径保证,与取整方向 floor/ceil 无关)。
- **向后兼容(输出等价,非调用路径等价)**:`surrender_rung_bp=None` 时 `eff_rung=rung`,`return a + rung·BP`。三分支输出与原实现逐一相同(surrender=True → `a+rung·BP`;surrender=False+min_profit>0 → `max(a,e·(1+mp))+rung·BP`;surrender=False+min_profit=0 → `a+rung·BP`)。**差异仅**:原实现 `min_profit==0` 时短路、不调 `surrender_sell`;新实现无条件先算 `surrender_sell` —— 输出不变,仅多一次纯函数求值(Codex P2,已澄清)。
- `entry=None`:`surrender_sell(a, None, rest_bps)` 返回 False → 不进斩仓分支,行为不变。

`final_sell_price(..., surrender_rung_bp=None)` 透传给 `sell_price_raw`;`min_sell_margin_bp` 地板仍由 `not surrender_sell(...)` 守卫(斩仓仍豁免,不变)。

### 3.2 配置(config.py + strategy.yaml)+ 每 symbol 影响 ⚠️

- `_STRATEGY_PARAM_DEFAULTS["surrender_rung_bp"] = None` —— **代码默认关**(旧行为),与 `sell_round:None`/`min_sell_margin_bp:0` 惯例一致。
- `config/strategy.yaml` 显式设 `surrender_rung_bp: 1` 启用。**回滚 = 删该行** → `None` → 恢复旧行为。

**放全局 `strategy:` 块 vs USD1 `universe` override —— 提请老板决策(§10)**。全局启用对各 symbol 的影响(Codex P1-4,必须透明列出):

| symbol | live? | rungs | 全局启用 surrender_rung_bp=1 的影响 |
|:---|:---|:---|:---|
| **USD1USDT** | ✅ live | [1,2,3,4,5] | 斩仓 5 slice 5.. 统一 anchor+1bp(**目标行为**) |
| **USDCUSDT** | ✅ live(Bitget) | [1](单档) | 单档斩仓本就 anchor+1bp → **零变化** |
| USDEUSDT | ❌ 仅回测 | [5,7,10,14,20]默认 | 回测斩仓 5–20bp → 统一 1bp(**回测口径变**,不影响真金) |
| USDTBUSDT | ❌ 仅回测 | [5,7,10,14,20]默认 | 同上 |

- 我的推荐:**放全局块**——契合老板既往「全局参数所有 symbol 统一」偏好([[rebuy-24h-hold-wip]] 明确覆盖 USDC-only 建议),且仅 USD1 生效于真金、USDC 零变化,其余仅回测。若老板只要 USD1 → 一句话改放 USD1 override。**加 config 测试锁定每 symbol 的 `surrender_rung_bp` 期望值**。

### 3.3 调用点透传(3 处 + status)

| 文件:行 | 改动 |
|:---|:---|
| `strategy_rules.py` | `sell_price_raw` / `final_sell_price` 加参数(§3.1);`rounded_sell_price` 不改(生产零调用,仅测试用) |
| `config.py` | `_STRATEGY_PARAM_DEFAULTS` 加 `surrender_rung_bp: None` |
| `config/strategy.yaml` | `strategy:` 块加 `surrender_rung_bp: 1`(位置见 §10 决策) |
| `live/engine.py:~346` | `self.surrender_rung_bp = _sp["surrender_rung_bp"]` |
| `live/engine.py:769` | `evaluate_fills` 的 `final_sell_price(...)` 加 `surrender_rung_bp=self.surrender_rung_bp` |
| `live/engine.py:899` | `_status_sell_price` 的 `final_sell_price(...)` 加同参 |
| `live/order_recon.py:110/127` | `desired_orders` 签名加 `surrender_rung_bp=None`;`final_sell_price(...)` 透传 |
| `live/engine.py:1792` | 调 `desired_orders(...)` 传 `surrender_rung_bp=self.surrender_rung_bp` |
| `backtest/strategy.py:157/165/208` | 无参 fallback dict 加 key;读 `surrender_rung_p=sp.get(...)`;`:208` 调用透传 |

**影响面已 grep 钉死**:`final_sell_price` 生产调用恰 4 处(全覆盖);`rounded_sell_price` 生产零调用;`tools/dashboard.py` 不直接算价、读 `status_doc` → 不改。

## 4. 正确性关键点(审查焦点)

1. **统一价** = `round_to_tick(anchor + surrender_rung_bp·bp, tick, sell_round)`,与 slice `entry`/`i` 无关 → 触发斩仓的 slice 严格同价(同 sell_round 口径下)。
2. **归因不受同价影响(Codex P1-5,已核实)**:5 同价单各有唯一 `order_link_id`(`sca-slice-i`);`match_live_orders` 按 link_id(第一优先级)归因、`diff_orders` 按 `slice_idx` 迭代(不按 price 合并)、engine 成交按 per-slice `order_id/filled_qty` 记账(engine:1531/1542)。approx `(side,price~)` 仅 link/id 都失配时的 last-resort,同价下退化为 ambiguous → `unattributed`(**安全 fail,绝不误塞给猜的 slice**,R2-P1)→ 加测试确认此退化不误 halt 正常运行。
3. **向后兼容**:`sell_price_raw` 参数默认 `None` → strategy_rules 旧单测零变化;仅生产调用点传 config 值(1)才启用。
4. **回测口径变化(必须多维量化)**:默认启用后回测斩仓卖价从各自 rung 降到统一 1bp(卖得更低/更早),会改变 fill 时点、持仓路径、rebuy 触发、interest min-snapshot、realized_capture 配对(Codex P2)。**验证阶段跑 before/after 多维对比**(见 §6),声明口径。
5. **min_sell_margin 交互**:斩仓仍 surrender → 仍豁免 margin floor(`not surrender_sell` 守卫不变)。
6. **PostOnly 风险(Codex P1-3,论证修正)**:`anchor` 是 1h EMA、**非盘口** —— `anchor+1bp` 不保证高于 best bid;把 2–5bp 降到 1bp 使卖价更接近盘口,**post-only reject 概率上升**。但 orders.py 有 `postonly_rejected → CLEAR → re-quote next reconcile` 兜底(engine:1836/1514),且**不计 reject streak、不触发 halt**(engine:1844)。斩仓本就求更快成交,1bp 是「更易撞成交」与「reject 重挂」的平衡 → 可接受;纯 anchor(0bp)cross 风险更高,故取 1bp。

## 5. 测试计划(TDD,先 RED)

- **strategy_rules / strategy_floor_rest**:
  - 斩仓 + `surrender_rung_bp=1` → `anchor+1bp`,**无视传入 rung**(rung=5 仍 anchor+1bp)。
  - 斩仓 + `surrender_rung_bp=None` → `anchor+原rung`(向后兼容);`min_profit=0+surrender+None` 边界零变化;`entry=None` 零变化。
  - 非斩仓 + `surrender_rung_bp=1` → 正常 rung 逻辑(不生效)。
  - **统一性**:`final_sell_price(anchor,rung=2,entry_a,…surr=1)` == `final_sell_price(anchor,rung=5,entry_b,…surr=1)`。
  - **阈值等号**:`anchor == entry*(1-rest_bps·BP)` 不 surrender(严格 `<`)。
- **order_recon**:`desired_orders(surrender_rung_bp=1)` 全 surrender → 所有 sell `Desired.price` 相等;**部分 surrender**(entry 不同)→ 只斩仓的同价、其余正常 rung;5 同价 desired → diff 后 5 个独立 place(不因同价合并)、第二轮 recon `leave`(不 churn);approx 归因在同价下退化为 unattributed 的确认测试。
- **engine_maker_fills**:`evaluate_fills` 斩仓统一价成交;部分成交按 link_id/order_id 归因回正确 slice。
- **config**:`strategy_for` 返回 `surrender_rung_bp`(默认 None;yaml=1;override 生效);**每 live symbol 的期望值锁定**(USD1=1、USDC=1)。
- **backtest_per_symbol**:回测斩仓统一价(参数注入)。

## 6. 验证

1. 增量:上述相关测试全绿(RED→GREEN)。
2. 四口径一致性:现有 backtest==live parity 测试不破(若因口径变化需更新,显式说明)。
3. **回测 before/after 多维对比**(Codex P2):PnL/年化 + fill count/timing + inventory days + interest + rebuy events + realized_capture rows;报告声明口径(adv/fill_mode)。
4. merge 前全量回归(基线 **709 passed**)+ Codex 异构 final review(有 diff)。

## 7. 回滚

删 `config/strategy.yaml` 的 `surrender_rung_bp: 1` 一行 → `None` → 全 symbol 恢复旧「斩仓各自 rung」行为。零代码回滚。

## 8. 替代方案(已否决)

- **物理合并 1 单**:与 5 单同价撮合/PnL 等价,却要重构 slice↔订单归因 + partial-fill 分摊,侵入「每 slice 独立」核心架构。老板 2026-07-11 基于等价性否决。
- **纯 anchor(0bp)**:PostOnly cross 风险更高(§4.6)。否决。
- **硬编码 `rungs[0]`**:不可配。用 `surrender_rung_bp` 参数化,可调 / 可关。

## 9. Codex 异构审查记录(gpt-5.5 xhigh,read-only)

无 P0。P1/P2 全部采纳并已回填本 plan:

| # | 严重度 | Codex 发现 | 处理 |
|:---|:---|:---|:---|
| 1 | P2 | "byte-identical" 过强(调用路径多一次 surrender_sell) | §3.1 改「输出等价」并说明 |
| 2 | P1 | 「floor」不符 —— 实际走 `sell_round`(live 可 ceil) | §1/§3.1/§4.1 改「sell_round 口径」,USD1=floor;统一性由同 sell_round 保证 |
| 3 | P1 | PostOnly「不 cross」论证错(anchor≠盘口) | §4.6 修正 + 指出 re-quote 兜底不 halt |
| 4 | P1 | 全局启用波及 USDE/USDT/BUSDT | §3.2 补全每 symbol 影响表 + §10 提请老板决策放全局/USD1-only |
| 5 | P1 | 同价冲击「价格唯一」假设 | §4.2 核实:归因走 link_id 不受影响,approx 退化为安全 unattributed;§5 加同价 diff/归因测试 |
| 6 | P2 | 回测验证只写 PnL 不够 | §6.3 扩多维(fill/inventory/interest/rebuy/realized) |
| 7 | P2 | per-slice 边界测试不足 | §5 加阈值等号 + 部分 surrender 混合测试 |

## 10. 待老板拍板(审批 gate 1)

1. **方向已对齐**(2026-07-11 三轮确认:统一价=anchor+1bp、5 单同价、可配 `surrender_rung_bp`)。本 plan 是实施细节确认。
2. **唯一新决策点(Codex P1-4)**:`surrender_rung_bp: 1` 放**全局 `strategy:` 块**(推荐,契合既往全局参数偏好;USD1 生效、USDC 零变化、USDE/USDT/BUSDT 仅回测口径变)还是 **USD1 `universe` override**(仅 USD1,最小波及)?→ **老板 2026-07-11 定:全局 `strategy:` 块**。

## 11. 实现完成记录(2026-07-11)

- **老板批准**:全局配置。已实现 6 生产文件(strategy_rules / config / strategy.yaml / order_recon / engine / backtest)+ 5 测试文件,严格 TDD(每功能点 RED→GREEN)。
- **全量回归:709 → 728 passed**(+19 新增 surrender 测试,零回归)。
- **⚠️ 纠正 plan §4.4 的一个错误假设**:我此前判断「USD1 真实数据从不斩仓」是**错的**(用固定 entry=1.0 估算)。真实 `entry` 是 rebuy 成交价(常 >1.0),阈值 `entry*(1-14bp)` 高于 anchor 最低 0.9988,**USD1 确实触发斩仓**。斩仓统一价对 USD1 有真实(小幅正向)效果,非 inert。
- **回测 before/after 实测**(声明口径:adv=0 + touch;记忆红线:口径变化不静默):
  | 口径 | before(斩仓各自 rung) | after(统一 1bp) | Δ |
  |:---|:---|:---|:---|
  | yaml floor+margin2(无参回测) | APR 3.891 / 749 sells | APR 3.955 / 759 sells | **+0.064 / +10** |
  | round+margin0(N5 legacy) | APR 2.661 / 328 sells | APR 2.684 / 337 sells | +0.023 / +9 |
  - 机理:斩仓时高档 slice 从各自 rung 降到统一 1bp → 提前离场 + 更低 rebuy → 多捕获 ~9-10 次。
  - **legacy rollback 基线 2.661 不动**(N5 params 无 `surrender_rung_bp` key → None);已更新 `test_no_args_follows_yaml_global_sell_round` 基线 3.891→3.955(带口径声明注释)。
- **Codex 异构 final review**(gpt-5.5 high,git diff):**无 P0/P1**。等价性 repr 级网格比对通过(4 分支无漂移),传参完整,`_S.get` 未配置得 None 符合 rollback。仅 3 个 P2(docstring 单位 `*BP` 漏写)→ **已全修**。
- **状态:✅ 实现+验证完成,待老板批准 merge main(gate 2)。**
