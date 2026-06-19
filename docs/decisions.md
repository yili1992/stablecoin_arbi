# Decisions

> 关键技术/策略决策及理由。最后更新：2026-06-19

## D1 — 只交易 USD1，砍掉 USDC/USDT
USDC/USDT 持有 0 息且价差 ~0；USD1/USDe/USDtb 才有 UTA 利息（USD1 10%）。USDC 纯价差腿实测 EV≈0。

## D2 — 不设价格止损，靠"必回锚"假设（带机制闸）
Owner 决策：稳定币脱锚会回。风险分层：足额储备币（USDC/USDtb）流动性脱锚会回；合成币（USDe）/算法币（UST→0，现为 USTC ≈$0.006）可能**永久死亡**。用机制闸（储备可验证才无脑买脱锚、USDe 设持仓上限+链上储备监控）替代价格止损。

## D3 — 浮动 EMA 锚切片阶梯（r1_6）= 推荐策略
原始**固定锚**策略输给持有（卖出后困在 0 息 USDT，carry 拖累）。**浮动锚**把 idle-USDT 压到 2.46%，样本内薄胜持有。

## D4 — 收益引擎是利息，不是价差
价差 edge 现实成交（adv≥0.5bp）下 ~0；参数优化（切片数×占比×rung，IS/OOS）**样本外无一配置跑赢持有**。结论：理性默认 = 持有 USD1 吃 10%。

## D5 — adv 只能实盘测，不能从 K 线测
OHLCV 测不出 adverse selection（被动单的真实成交/排队/逆选）。`tools/dryrun.py` 在服务器用 WS markout 实测，是唯一能把 edge 大小收敛成实数的实验。

## D6 — 架构参考 boros_strategy
config/yaml 单源 + src/ 包 + docker profiles + entrypoint dispatcher + docs(conventions/decisions) + tests，按 Python 与本项目规模等比例采纳（不照搬 TS / server / web）。

## D7 — paper 引擎取代 dryrun 成为默认常驻服务
把 dryrun（纯 markout 实测）升级为 `live/engine.py` paper 引擎：在 Bybit PUBLIC WS 实时数据上**模拟**推荐切片阶梯策略，复用 backtest 同源规则（`paper == backtest`），并写出 dashboard 需要的全量状态（仓位/切片/指标/K 线/PnL/成交质量）到 `status_<symbol>.json`。docker compose 默认服务从 `dryrun` 改为 `paper`（+ dashboard），tools 仍在 profile。dryrun 作为 CLI 命令保留。保留 markout 作为真实 edge 仪表（D5 结论不变）。

## D8 — live 模式三重安全闸（默认 paper，绝不误下单）
live 真实下单脚手架化但严格 gated：必须 `mode==live` **且** `LIVE_TRADING_CONFIRM=="yes"` **且** `BYBIT_API_KEY`/`BYBIT_API_SECRET` 齐全，否则明确报错拒绝；`config live.max_order_usd` 每单名义硬上限。paper 是 CLI/compose/config/.env 各处默认，零密钥零下单。理由：稳定币策略样本外不胜持有（D4），实盘价值未证，宁可多道闸也不让任何路径意外触发真实交易。

## D9 — dashboard 改中文 + K 线蜡烛图
dashboard 从 dryrun 文本面板升级为中文界面 + candlestick 图，读 paper 引擎的 status JSON 富展示仓位/指标/PnL/成交质量。诚实红线：仅样本内薄胜、样本外不胜，**界面不得暗示稳赚**。

## D10 — paper/live 引擎崩溃安全持久化 + 重启 resume
**问题**：容器/进程重启曾清空全部历史交易数据。根因不是 volume 没挂（compose 一直挂着 `./out`），而是引擎纯内存起步、**启动不 reload**、重启后 12s 内用空内存截断覆盖 `status_<symbol>.json`（CSV 只在跑完时写）——重启即自动清盘。

**修复**：引擎在**每笔成交**和每次 status 写时**同步**把完整可重建状态（slices / realized_capture / `DailyMinInterest` 全部内部态 / start / anchor·ema·last_1h_start / history）原子写入 `out_dir/<symbol>_state.json`；成交事件 append 到 `out_dir/<symbol>_events.jsonl`（append-only 审计 + CSV 源）。启动 `_maybe_resume()` 读快照恢复，`bootstrap()` 不再重置已恢复的持仓。**快照同步先于流水** ⇒ 快照永远 ≥ 流水 ⇒ resume 只读快照、不回放。开关 `live.persist`（默认 true）；`--seconds 0` = 永久跑（live 常驻）。`out_dir` 必须是持久化挂载。

**取舍**：markout 量规（`done`）不持久化——其 `{horizon:bp}` 用 int 键，JSON 化会变 str 键、废掉 `aggregate_markout` 的 `mo.get(30)`；markout 是测量量，重启后从实时流数十秒自重建，可接受。

**向后兼容**：无快照文件 或 `persist=false` ⇒ 与旧行为逐字节一致；`status_doc` 键不变（dashboard 契约）。

**live 安全前置（接真实下单前必须，未做）**：
1. **R1 — 交易所对账**：本地 state 对**真实**下单必要但**不充分**——宕机期间可能发生本地文件不知道的真实成交。"corrupt/缺失 state → 全新部署"对 live 是 **fail-OPEN**（无视交易所真实仓位铺新阶梯），今天仅因成交是**模拟**的而安全。翻开真实下单前，启动**必须**先与交易所对账（查真实余额+挂单）才能信任本地持仓，corrupt/missing-state 路径必须 gate 在对账之后。
2. **fail-closed 落盘语义（Codex 异构审 P1#3）**：当前 `save_state` 在 fill 路径失败被吞+继续（`[PERSISTENCE ERROR]` 日志），fill 只在内存——崩溃前未落盘会丢/重放该 fill。对 paper 是可接受的退化；对**真实下单**必须改 **fail-closed**：落盘失败即停/禁用 fills 或 retry-until-durable（在 reconnect 循环之外）。
参见 D8 三重安全闸。

**损坏/畸形文件容错（已修，本轮 + Codex 复审闭环）**：`load_state`（非UTF8/坏JSON/perms→None）、`read_events`（损坏 ledger→保留已解析前缀，不崩）、`_maybe_resume`（缺键 *和* 错类型 v=1 → log + 全新启动，原子 locals-first 不留半套）。"corrupt/malformed → fresh start，绝不 boot 崩溃" 契约现已完整覆盖。

## D11 — 执行模型重新锁定 TAKER → MAKER（取代 R1 的 taker 锁）
**决策**：Phase 3 的执行模型由 owner **明确重新锁定为 MAKER**（PostOnly 挂单阶梯），**取代** `docs/live-bybit-readonly-r1-plan.md` 中记录的旧 TAKER（IOC marketable-limit）锁定。Phase 3a 不再重开此决策。

**理由**：
1. **rung 价格是确定的**：挂单价由 `R_i = anchor + rung_bp_i/1e4`（卖）/ `B = anchor + rebuy_off_bp/1e4`（买）唯一确定，且 anchor 只在**收盘 1h K 线**上更新——所以在已知档位预挂 PostOnly resting limit 是完全可规约的，不存在 taker 的"成交价不确定/穿价"问题。
2. **赚价差而非付价差（capture-not-pay spread）**：maker 被动挂单**捕获**半价差（不付出），在成交概率足够时严格优于 taker 主动穿价。0-fee 去掉的是**手续费**，不是**价差**——taker 仍要付半价差，maker 则反过来赚它。
3. **adv 只能实盘测（验证留给 3b）**：被动单的真实成交概率 / 排队 / 逆选（adverse selection）OHLCV 测不出（见 D5），是否真有正 edge 由 **Phase 3b** 用实盘 markout 实测收敛。3a 只交付**安全的 maker 原语 + 管线 + 声明式对账**（merge-ready，非 live、不声称策略经济性）。

**与 R1 的衔接（代码半边）**：R1 的"任何挂单 ⇒ 异常"规则建立在 taker 不留挂单的前提上；maker 策略**按设计**会留 resting 挂单，因此 `reconcile.py` 必须改为 maker-aware（接受我们 own 的 `link_id`/预期挂单为正常，仅对 off-strategy 挂单 refuse）。该 reconcile 改动（Phase 3a Task 3）是本治理决策的**代码半边**。

**取舍**：maker 放弃"立即成交"换"赚价差 + 保排队"；可能不成交（PostOnly 被拒 → 下一 tick 重新报价，带 per-slice 冷却），这是可接受的退化。3a 全部真实下单仍 testnet-only 且在既有三重闸 + R1 对账闸之后。
参见 D5（adv 只能实盘测）、D8（三重安全闸）、D10/R1（交易所对账）。
