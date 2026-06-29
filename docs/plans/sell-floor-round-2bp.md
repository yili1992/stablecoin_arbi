# Plan: 卖价取整 ceil→floor + 非斩仓 ≥2bp 地板(config 门控)

## 1. 目标 / 用户原话

把卖单价格的 tick 取整从 **ceil(向上)** 改成 **floor(向下)**,使挂单更易成交;
但加一个保底:**非斩仓场景下,卖价至少比成本高出 2bp**。

用户精确语义:
- "这里只是把向上改成向下" —— 核心唯一改动 = ceil→floor,其余(`min_profit_bp=1`, `rung=1`)不动。
- "算出来 1bp → 改为 2bp;算出来 3bp → 不修改" —— 即对最终价取 `max(floor结果, 成本+2bp)`。
- "只针对非斩仓的场景" —— 斩仓(`rest_bps` surrender)豁免该地板,可亏卖出场。
- "这个回测的 APR touch 是准确的" —— 认可 touch 口径;要求回测与实盘**同口径**(否则回测虚高)。

**范围(2026-06-29 老板定):USDC + USD1 都改 → 作为全局默认。** 两个 live 标的都切到新卖逻辑;
非 live 标的(USDE/USDTB)回测口径也随之改(无真金影响)。USD1 是真金在跑 → 上线需 canary 观察。

### 离线验证(adv=0,已实测)
| 标的 | 现状 ceil(实盘) | 新 floor+2bp | 说明 |
|---|---|---|---|
| USD1USDT(carry 8%) | 9.23t / 7.85s | **10.27t / 7.64s** | carry 主导,touch+1.0/strict−0.2,影响小 |
| BGUSDCUSDT(carry 0) | 3.28t / 0.59s | **9.97t / 0.80s** | 价差全靠成交,touch~3× |

## 2. 现状(已勘探,grounded)

卖价 = `sell_price_raw(anchor,rung,entry,min_profit_bp,rest_bps)` 再取整。`sell_price_raw`
(`strategy_rules.py:28`)已内置 surrender:`surrender_sell` 为真时禁用 floor、`raw=anchor+rung`。

四个取整调用点:
| 用途 | 位置 | 现取整 |
|---|---|---|
| **实盘下单价**(真金) | `order_recon.py:123-125` `desired_orders` | `quantize_price("sell")` = **ceil** |
| **看板显示价** | `engine.py:864-874` `_status_sell_price`(live 分支) | **ceil** |
| 回测成交价 | `backtest/strategy.py:198` | **round** |
| paper/dryrun 模拟成交 | `engine.py:744` `evaluate_fills` | **round** |

关键事实:
- 实盘=ceil、回测=round → **本就不一致**;USDC 回测(round)9.97% 远高于实盘(ceil)3.28%(已实测)。
- `desired_orders` 已有 `s.get("entry")`(成本)可做地板;有 `bid`、`tick`。
- PostOnly 穿越**已有兜底**:`orders.py` 把 `postonly_rejected` 归类为"重挂下一 tick",engine 有 reject-streak 冷却/halt(F9)。
- `tick = 1e-4 = 1bp`(价格~1.0),故 "2bp" = 2 ticks = `entry + 2*BP`。

## 3. 设计

### D1. 单一真源(消除 round/ceil/floor 漂移)
在 `strategy_rules.py` 新增:
```python
def round_to_tick(x, tick, mode):           # mode ∈ {"round","ceil","floor"}
    ...                                       # 带 1e-7 epsilon 防浮点边界

def final_sell_price(anchor, rung_bp, entry, min_profit_bp, rest_bps, tick,
                     *, sell_round="ceil", min_sell_margin_bp=0.0):
    raw = sell_price_raw(anchor, rung_bp, entry, min_profit_bp, rest_bps)
    px  = round_to_tick(raw, tick, sell_round)
    # 非斩仓地板(surrender 豁免);entry 缺失时跳过
    e = _finite(entry)
    if min_sell_margin_bp > 0 and e is not None and not surrender_sell(anchor, e, rest_bps):
        px = max(px, round_to_tick(e * (1 + min_sell_margin_bp*BP), tick, "floor"))
    return px
```
backtest 与 live 都调它 → 不可能再漂移。`rounded_sell_price`(round)保留为薄封装或迁移调用方。

### D2. config:全局默认即新逻辑(yaml 单源)
`strategy:` 全局块新增两参(`strategy_for` 已 dict-merge;per-symbol 仍可 override 但本次不需要):
- `sell_round: floor`(全局默认;原 ceil/round 全部由此取代)
- `min_sell_margin_bp: 2`(全局默认;非斩仓 ≥2bp 地板)

**保持各标的现有 `rungs`、`min_profit_bp` 不变**(用户:只改取整 + 加地板)。USD1 用全局 rungs[1..5]、USDC 用其 override rungs[1] —— 都继承全局 `sell_round:floor` + `min_sell_margin_bp:2`。
非 live 标的(USDE/USDTB)同样继承(仅回测,无真金)。

### D3. surrender 豁免
地板仅在 `not surrender_sell(anchor, entry, rest_bps)` 时套用。surrender 时 `final_sell_price`
= floor(anchor+rung),无地板 → 可低于成本斩仓。复用既有 `surrender_sell`,阈值 `rest_bps=14` 不变。

### D4. 回测保真(用户的"回测准确"诉求)—— 全口径统一
**4 个取整调用点全部改调 `final_sell_price`,统一读 config 的 `sell_round`/`min_sell_margin_bp`**:
- `desired_orders`(实盘下单)、`_status_sell_price`(看板)、`evaluate_fills`(paper 模拟)、`backtest/strategy.py`(回测)。
- 全局默认 floor+2bp → **backtest == live == paper == dashboard,所有标的同口径**,彻底消除原 round/ceil/round 三处漂移。
- 这正是用户要的"回测准确":USDC 回测 9.97% = 实盘将跑的口径;USD1 回测 10.27t = 实盘口径。

> 副作用:USD1 回测 headline 由 round 9.34 → floor 10.27(更准,非虚高)。`strategy.py` docstring 的
> HONEST FINDING 旧数字(touch~10/strict8.9)需同步更新为新口径。非 live 标的回测数字也随之刷新。

### D5. PostOnly 穿越安全
floor 使卖价低 ~1 tick,贴近 bid 时 PostOnly 拒单概率略升。**依赖既有兜底**(reject→重挂、streak halt),
不新增 guard(新增 `sell≥bid+tick` 会压制合理的低挂,违背初衷)。列为监控风险:观察 reject streak。

### D6. 看板一致
`_status_sell_price`(engine.py:864)改调 `final_sell_price`(同 sell_round/min_margin)→ 显示的"卖出目标" == 真实挂单价。

### D7. "2bp" 定义
地板 = `round_to_tick(entry*(1+2bp), tick, "floor")` = entry 上方第 2 个 tick(成本 1.0010 → 1.0012)。
匹配用户"1.0012"直觉。注:依赖 `tick≈1bp`(本对成立);跨标的用乘法+floor 保证落在"≥2bp 的最高合法 tick"。

## 4. 全场景矩阵(=测试用例)

| # | 场景 | 输入(cost=1.0010) | 期望卖价 |
|---|---|---|---|
| 1 | 非斩仓,锚绑定,floor | anchor1.00116 mp1 rung1 floor | 1.0012 (+2bp) |
| 2 | 非斩仓,floor 出 1bp → 抬 2bp | anchor1.00115 mp1 **rung0** floor | 1.0012 (+2bp) |
| 3 | 非斩仓,floor 出 3bp → 不动 | anchor1.00125 mp1 rung1 floor | 1.0013 (+3bp) |
| 4 | **斩仓**(anchor<成本-14bp)→ 豁免地板,亏卖 | anchor0.9994 floor | 0.9995 (-15bp) |
| 5 | `sell_round=ceil`(USD1 默认)→ 与现状字节相同 | 任意 | == 现 ceil |
| 6 | `min_sell_margin_bp=0`(默认)→ 无地板 | 任意 | == 纯 floor/ceil |
| 7 | **回测 USDC floor == 实盘 USDC floor**(parity) | 同 anchor/entry | 数值相等 |
| 8 | PostOnly 穿越 → reject → 重挂(既有路径) | sell≤bid | postonly_rejected |
| 9 | tick 浮点边界(1.00126 floor) | — | 1.0012(非 1.0011/1.0013) |
| 10 | entry=None(空仓位)→ 跳过地板 | entry=None | 不崩,无 clamp |
| 11 | 看板 sell_target == desired 下单价 | 同 state | 相等 |

## 5. 改动文件

| 文件 | 改动 |
|---|---|
| `src/sca/strategy_rules.py` | + `round_to_tick`、`final_sell_price`(含 surrender 豁免地板) |
| `src/sca/live/order_recon.py` | `desired_orders` 增 `sell_round`/`min_sell_margin_bp` 参;123-125 改调 `final_sell_price` |
| `src/sca/live/engine.py` | `_status_sell_price`(864)、`evaluate_fills`(744)、`desired_orders` 调用处:读 `self.sell_round`/`self.min_sell_margin_bp`(源自 `strategy_for`)并透传 |
| `src/sca/backtest/strategy.py` | 198 改调 `final_sell_price`,读 `sp` 的 `sell_round`/`min_sell_margin_bp`;**docstring HONEST FINDING 数字更新为 floor 口径** |
| `src/sca/config.py` | `_STRATEGY_DEFAULTS`/`strategy()` 增 `sell_round`、`min_sell_margin_bp` 两键 |
| `config/strategy.yaml` | **全局 `strategy:` 块**增 `sell_round: floor` + `min_sell_margin_bp: 2`(两 live 标的+其余全继承) |

测试(更新+新增):`test_strategy_rules`、`test_strategy_floor_rest`、`test_order_recon`、
`test_engine_maker_fills`、`test_backtest_per_symbol`、`test_config_strategy_for`、`test_config_strategy`、`test_dashboard`、`test_smoke`。
**`test_smoke` 的 USD1 canary 不变量数字会变(round→floor),需按新口径更新断言。**

## 6. 验证

- 11 场景 TDD 红→绿。
- **parity 断言**:同一 (anchor,entry,tick) 下 backtest 路径与 live 路径的 USDC 卖价逐点相等(场景 7)。
- **零回归**:USD1 全测试不变;`test_smoke` 的 canary 不变量保持。
- 重跑 USDC 回测确认 floor+2bp = touch 9.97% / strict 0.80%(已离线验证,实现后复跑对齐)。
- 全量 `pytest`(现 ~505 测试)绿。

## 7. 风险 / 回滚

| 风险 | 缓解 |
|---|---|
| floor 致 PostOnly 拒单增多 | 既有 reject→重挂 + streak halt;上线后看 reject 计数 |
| 部署瞬间老挂单(ceil 价)≠新 desired(floor)→ 一次 cancel+replace | 一次性、良性;卖侧本就 ≥1tick 重挂 |
| clamp 与 surrender 边界错配(把斩仓也套地板=死扛) | 场景 4 测试钉死;复用 `surrender_sell` 同一谓词 |
| **USD1 真金行为变化**(ceil→floor,实盘在跑) | 离线已量化(carry 主导,touch+1/strict−0.2,低影响);上线 canary 观察成交/reject;改的是数据口径非控制流 |
| 部署瞬间 USD1×5 + USDC 老挂单(ceil 价)重挂到 floor | 一次性 cancel+replace,良性 |
| 浮点 tick 边界 | `round_to_tick` 带 epsilon;场景 9 钉死 |
| 全局默认改→波及非 live 标的(USDE/USDTB)回测 | 仅回测无真金;口径统一反而更一致 |

**回滚**:全局 `strategy:` 删两行(`sell_round`/`min_sell_margin_bp`)→ 需把代码默认设回原口径
(live=ceil、backtest/paper=round)才能完全复原。**实现时 `final_sell_price` 的 `sell_round` 默认值设为各调用点的原口径**
(desired/status=ceil、backtest/evaluate=round),保证"删 yaml 两行 = 100% 回到今天的行为"。

## 8. 执行模式

7 文件但**高度耦合**(一条"卖价单源"主线,parity 是核心)。拆 subagent 会引入口径漂移风险 →
**建议紧凑模式**(Dev-Lee 自身 TDD),逐场景红绿,最后整体 ce/qa/Codex 审。

## 9. 待老板拍板(硬 gate:动手前对齐)

- ✅ **方向**(已确认):ceil→floor + 非斩仓 ≥2bp(算1bp→2bp/算3bp不动)+ 斩仓豁免。
- ✅ **范围**(已确认):USD1 + USDC 都改 → 全局默认。

仍需确认:
1. **USD1 真金**:USD1 实盘卖单会从 ceil 变 floor+2bp(离线:touch 9.23→10.27、strict 7.85→7.64,carry 主导低影响)。上线我会 canary 盯成交/reject —— 接受?
2. **新参命名**:`sell_round`(值 floor/ceil/round)、`min_sell_margin_bp`(=2)—— OK?
3. **模式**:7文件高耦合(parity 是命脉),建议**紧凑模式自己 TDD**(拆 subagent 易引入口径漂移)—— 同意?
