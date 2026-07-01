# 设计文档:买单 24h 时间钉死改单(rebuy time-hold)

- 日期:2026-07-01
- 分支:`worktree-dev-rebuy-24h-hold`
- 状态:待老板 review

## 1. 背景与问题

USDC@Bitget live 连续数小时**零成交**(`filled=0`, `realized=0`)。实测定位根因(非 bug、非连接、非改价逻辑):

- **厚盘口 + 追跌**。当前买价公式 `rebuy = floor(min(anchor, bid) − 1bp)` 让买单**永远躲在 bid 下方一档**、每 tick 追 bid 下移。
- 实测 Bitget USDC/USDT 盘口深度:`1.0009` 档 **625 万 USDC**、`1.0008` 档 20 万。我们 1000 USDC 的被动买单按**价格-时间优先**排在队尾,要 625 万+ 的单向 taker 卖压才轮到 → 几乎不可能。
- 对照 USD1@Bybit(盘口十万级、薄一个数量级)正常成交,证明差异在**盘口结构 + 追跌策略**,不在代码。

**核心矛盾**:追跌 = 买单一路躲在价格下方、永远差一档、卖方永远够不到 → 零成交。

## 2. 目标

让买单**钉住不动**,逼价格下跌时来撞它、拿成交;同时不牺牲 maker(0-fee)身份、不倒挂。

**非目标**:不改卖腿(卖价维持 `成交价 + 2bps`);不追求回测证明 edge(队列价值回测测不出,见 §7)。

## 3. 设计

### 3.1 买价公式

```
rebuy = floor( min( anchor − 1bp ,  ask − 1tick ) )
```

- `anchor − 1bp`(`rebuy_offset_bp = −1`):主锚。anchor = EMA21@1h,**每小时才动**,天然稳定 → 买价天然 sticky。
- `ask − 1tick`:上限,保证买价永远在 ask 下方 → 不会变 taker、不 PostOnly reject。
- **回测 fallback**:回测无实时盘口(`ask = None`)→ 退化为 `floor(anchor − 1bp)`,**回测行为逐字节不变**(守 backtest-fidelity 铁律,单测锁定)。

相比旧公式:去掉 `min(anchor, bid)` 里的 **bid 追随**(每 tick 抖动的追跌根源),改由 anchor 主导 + ask 兜顶。

### 3.2 改单触发(核心:时间钉死)

```
target = floor(min(anchor − 1bp, ask − 1tick))
若 target == 当前挂单价        → 不改
若 target ≠  当前挂单价        → 不立即改,等此单挂满 rebuy_min_hold_sec(默认 24h)才改
```

- **24h 内钉死不动**,不管 anchor/盘口怎么变。
- **计时起点** = 此单挂上的时刻(`last_place_ts`);一旦改单,重置计时。
- **首次挂单不受冷却**:slice 从"无活单"到"有活单"(新 slice、成交后重挂、reject 后重挂)**立即挂**,并记录 `last_place_ts`。冷却只约束"已有活单 → 想换价"。

### 3.3 为什么这解决零成交(关键论证)

买单钉死在某价 P 不动 → 价格下跌时**必然穿过 P**(有人在 P 卖 = 吃到我们的挂单)→ **成交**。

对比旧追跌:买单一路躲在价格下方,P 跟着 bid 下移,永远差一档、永远碰不到。**钉死 = 逼价格来撞我们。** 时间维度 hold 比价格维度(band)更强:band 下跌超阈值仍会追,时间钉死则在窗口内绝不动。

### 3.4 边界与保护

- **去掉 3bp band**(`reprice_tol_bp` 的买侧迟滞):band 原是挡 bid 每 tick 抖动的;改 anchor 主导后 bid 抖动不再影响买价,band 失去意义,由 24h 冷却取代。
- **去掉主动让位**(旧"挂单价 ≥ ask 立即改"):已挂买单被价格触及是**成交**(要的),不是 reject;新挂/改单时 `ask − 1tick` 已保证不 taker。故不需要主动让位。
- **reject / cooldown 兜底保留**:万一盘口跳变导致 PostOnly reject,现有 cooldown 机制仍生效。
- **买侧下限** `rebuy_floor_px = 0.9990`:算出的买价 < 此值 → **停止挂买单**(撤出等价格回来),防脱锚急跌时一路接刀。用绝对底而非 `anchor − N bp`(anchor 每小时才动、急跌时滞后,相对底会失效)。
- **已知代价**:24h 内若急跌,钉死的买单成为最高买价、可能成交在相对高位(买贵一点)。对稳定币(锚定 1.0 附近)+ `rebuy_floor_px` 兜底可接受;这正是"用可能买贵一点换成交",契合当前"要成交"的目标。

## 4. 配置项(`config/strategy.yaml`)

| key | 默认 | 说明 |
|---|---|---|
| `rebuy_min_hold_sec` | `86400` | 买单改价的最小钉死时长(秒,24h)。live 可调,便于观察 |
| `rebuy_floor_px` | `0.9990` | 买侧下限;算出价 < 此值则停止挂买 |
| `rebuy_offset_bp` | `-1`(现有) | 买价相对 anchor 的偏移,保留 |
| `reprice_tol_bp` | 现有 | 买侧 band 移除后**不再被使用**(卖侧用 1-tick 判定、本就不依赖它);保留配置键防兼容,标记 deprecated |

## 5. 影响范围(downstream)

- `strategy_rules.py :: rebuy_price_raw`:签名增 `ask` + `tick`,公式改为 `min(anchor−1bp, ask−1tick)`,`ask=None` fallback。
- `live/order_recon.py :: desired_orders`:改传 `ask`(替代/补充 `bid`);买单改价判定接入 24h 冷却。
- `live/engine.py`:reconcile 传 `self.ask`;新增 per-slice `last_place_ts` 持久化状态;`_status_rebuy_price` 同源调用(dashboard 显示自动一致);移除买侧 band/让位路径。
- `live/engine.py` 状态持久化:`last_place_ts` 写入 durable state(跨重启保留,否则重启后 24h 重新计)。
- **backtest**:`ask=None` fallback 保证 headline 不变(单测锁定)。dashboard `_status_rebuy_price` 与实际下单同源 `rebuy_price_raw` → 显示与挂单一致。

预估改动文件(~5):`strategy_rules.py`、`live/order_recon.py`、`live/engine.py`、`config/strategy.yaml`、`tests/`。

## 6. 状态(新增持久化字段)

per-slice `last_place_ts`(float, epoch 秒):此单挂上的时刻。挂单/改单成功时写入,`_persist_durable_or_halt` 落盘。旧快照无此字段 → 加载时默认 `None`/`0`(视为"可立即改",不阻塞首个改单周期)。

## 7. 验证方法(老板拍板:单测 + live 观察)

- **回测测不出这个改进**:回测 fill 是 touch(`hi >= R`,价格触及即成交、不建模排队),队列价值无法离线验证,甚至可能反向显示 hold 更差(注释已述"canary probe for fill rate / queue loss")。故**不以回测为 gate**。
- **单测**:保证 24h 钉死 / 公式 / fallback / 下限等**逻辑正确性**。
- **live 观察**:USDC@Bitget 厚盘口零成交环境作天然观察床,上线后看成交率是否改善。
- 诚实边界:本改进**不假装回测证明了 edge**,只证逻辑正确 + live 观察成交率。

## 8. 单测清单

1. 定价-anchor 主导:ask 远高 → `rebuy == floor(anchor−1bp)`
2. 定价-ask cap:ask 逼近 → `rebuy == floor(ask−1tick)`
3. 定价-backtest fallback:`ask=None` → `floor(anchor−1bp)`(== 旧行为,回测不变)
4. 24h 钉死:目标 ≠ 挂单价但未满 24h → **不改**(leave)
5. 24h 到期:挂满 24h 且目标 ≠ 挂单价 → **改**,重置计时
6. 目标 == 挂单价:任何时间都不改
7. 首次挂单立即:无活单 → 立即挂(不等 24h),写 `last_place_ts`
8. 改单重置:改单后 `last_place_ts` 更新
9. 下限:`rebuy < rebuy_floor_px` → 不挂买单
10. reject 兜底:PostOnly reject → cooldown 仍生效(未被移除)
11. 旧快照兼容:state 无 `last_place_ts` → 默认视为可改,不崩

## 9. 回滚

- 改动集中在 `rebuy_price_raw` + 买单改价判定;`rebuy_min_hold_sec` 调 `0` 可近似退回"实时改"(但公式已换 anchor 主导,非逐字节回旧)。
- 彻底回滚 = revert 本 worktree 的 merge commit。
- 卖腿、reject/cooldown、下限之外的路径均不动,爆炸半径小。
