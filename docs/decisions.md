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

## D12 — Phase 3b：mainnet 小额 canary 启用（双确认闸 + 资金上限 + max-loss 熔断）
**决策**：3b 在已合并的 3a maker 层上做一个**紧凑 DELTA**——只动 **闸 / 配置 / sizing-cap / kill-switch**，**绝不改** maker 订单生命周期（reconcile / poll / cancel-to-terminal / persistence，feedback_multi_mode_parity）。交付 = **merge-ready，非 merge、非 run**；真金 canary 跑由 owner 单独执行（提供 keys + 设 canary env）。testnet 不再是必经路径（owner 决定 dryrun-或-live；dryrun 已跑），但 testnet 路径保持默认、零变化。

**四个真金要点**：
1. **mainnet 双确认闸（additive，testnet 仍默认）**：新增 `config.resolve_allow_mainnet` = `runtime.allow_mainnet==true`（config，默认 false）**AND** env `LIVE_MAINNET_CONFIRM=="yes"`（**新** env，区别于 3a 的 `LIVE_TRADING_CONFIRM`）。`_compute_maker_enabled = armed AND resolve_maker_enabled() AND (resolve_testnet() OR resolve_allow_mainnet())`。3a 全部闸（mode=live + LIVE_TRADING_CONFIRM + keys present + fresh_deploy 无条件拒）不变；mainnet 只是把 `testnet` 要求换成更严格的 `allow_mainnet` 双确认。`MakerOrderClient` 构造/place 在 mainnet 上**仍硬拒，除非注入 allow_mainnet**——任一层 opt-in 缺失都不能让 mainnet 单漏出去。
2. **total-alloc canary cap 真正在 sizing 生效**（arb-execution-risk：配了却不在 live sizing 路径应用的 cap = 真缺陷）：新增 `live.max_total_alloc_usd`（USD；`-1` ⇒ 不设上限 = 用全钱包，对应老板的"用钱包里所有的钱"）。`_seed_slices_from_balance` 用 `deployable = min(amt, cap/mark)` 切片（cap<0 走全钱包）；`_available_from_balance`（reconcile re-quote 的 sizing 池）也按 cap 收口，使 partial fill 后 re-quote 不超 canary 总额。**funded 子账户须按 canary 额度精确充值**（over-funded 会在 R1 dedicated 精确对账处 refuse——保守，符合预期）。
3. **per-order `max_order_usd = -1` 解除**：`order_recon._clamp_to_cap` 与 `orders.place_postonly` 硬断言在 `max_order_usd < 0` 时跳过（无 per-order cap）；正值仍照常 clamp + assert（回归）。**危险**：`-1` 移除了对 garbage-size 单的最后一道防线，仅在 canary 验证完 sizing 后用，**run #1 绝不用**。
4. **max-loss kill-switch（新，真金强制）**：新增 `live.max_loss_usd`（USD；`0`/`-1` ⇒ 禁用；shipped 默认 0 以保 testnet/paper 零变化，canary run 必须开，如 50）。每个 maker_step（`poll_fills` 记账后、`reconcile_orders` 下新单前）用 **partial-aware `_slice_value`**（slice 可同时 qty>0 且 cash>0）算 mark-to-market equity；若 `start_equity − equity >= max_loss_usd` ⇒ 走 3a 安全 halt：`_cancel_all_resting`（cancel-to-terminal，非裸撤）+ 置 `_halted` + 拒后续下单 + 响亮 log。**无自动 reset**（重启 + 人工 root-cause）。`_start_equity` 锚定到 resume 时刻的 **当前 mark-to-market**（非 config alloc，避免小 canary 在大 alloc 上误熔断）。pre-trade & atomic（trading-risk-control）；**绝不 fail-open**。**熔断的跨重启持久化见 D13**（对抗审纠正：原 3b "不持久化基线" 是失血漏洞）。

**markout（3b 的目的）**：3a 已有 adverse-selection gauge，且 `_on_trade_markout` 在 maker 路径上照常喂（`_handle` 中位于 maker 分支之外），status 已输出 markout——3b 仅加测试钉住"live 路径会真的记 markout 数据"，无新增生产代码。

**理由**：真金需要 pre-trade、atomic、fail-closed 的风控与 kill-switch（ai-quant-validation production gate：halt = flatten/stop，reset 须人工 root-cause）；mainnet 须**两个独立 opt-in**，一个开关动不了真钱；cap 必须在**真实 sizing 路径**强制（不只是存配置）。
**取舍**：total-alloc 池上界用 $1 稳定币近似（seed 用 wallet USD mark 精确，池用保守 per-side `min(avail,cap)` 上界，永远只收紧不放宽——单边 seed 使该 per-side 上界 ≈ 全 canary 额度，故安全，非真·剩余预算共享）。（原"基线重启后重锚、不持久化"取舍已被 D13 推翻。）
参见 D8（三重安全闸）、D10/R1（交易所对账）、D11（maker 执行模型）、**D13（对抗审纠正）**；arb-execution-risk / ai-quant-validation / trading-risk-control。

## D13 — Phase 3b 对抗审纠正：熔断必须跨重启持久 + mainnet 启动护栏（真金）
**背景**：D12 的 3b maker 层经对抗审（adversarial review）发现 1×P0 + 3×P1，均围绕"配置正确但真金路径上仍可失血/裸跑"。本决策记录纠正，不改 maker 订单生命周期（feedback_multi_mode_parity）。

1. **P0 — max-loss 熔断跨重启持久（最关键，纠正 D12）**：D12 原决策"`_start_equity` 不持久化、重启后重锚（重启即人工介入点）"是**错的**——docker `paper` 服务带 `restart: unless-stopped`，**重启是自动的、非人工**。后果：重启后 `_halted→False` + `_start_equity` 重锚到已缩水 equity ⇒ 回撤预算归零 ⇒ 再亏一整个 `max_loss` 的**失血循环**。**修**：`_halted` 与 `_start_equity` 写入 `_state_dict`（**additive，schema 仍 v=2**，旧快照缺键时 `.get` 默认 `_halted=False`/`_start_equity=None`，照常 resume 不走 fresh）；`_halt_operator_reconcile` raise 前**持久化** halt（best-effort，`persist` 关或 dead-disk sweep 时跳过）；resume 后**不重锚**基线（downtime 亏损也算进预算）。重启若 `_halted==True` ⇒ `_guard_resumed_halt` **响亮 SystemExit 拒绝进入 maker 路径**，清除须显式 `LIVE_CLEAR_HALT=yes`（保仓位、重锚基线避免立刻再熔断）或删 state 文件（全新）。docker：真金走裸机 `sca live`，`paper` 容器自动重启因"持久化+恢复拒绝"已不再续跑交易（见 docker-compose.yml 注释）。
2. **P1 — mainnet canary 护栏**：`max_loss_usd=0`（无熔断）+ `max_total_alloc_usd=-1`（全钱包）是 shipped 默认，双确认上 mainnet 即"全钱包裸跑无止损"。**修**：`_guard_mainnet_canary` 在 `resolve_allow_mainnet()` 为真时 **SystemExit 拒绝，除非** `max_loss_usd>0`（真金熔断**强制、无豁免**）**且**（`max_total_alloc_usd>0` **或** `-1`+env `LIVE_UNCAPPED_CONFIRM==yes` 第三道显式确认）。testnet/paper 不受约束（默认旋钮在非 mainnet 仍合法，零行为变化）。
3. **P1 — 启动 banner 按真实 venue**：原 banner 硬编码 `(TESTNET)`，mainnet 上撒谎。**修**：`_maker_startup_banner` 按 `resolve_allow_mainnet()` 分支；mainnet 打**响亮 REAL-MONEY MAINNET 警告 + 当前生效 caps**（total-alloc / per-order / max-loss）；testnet 保持原文案。
4. **P2（顺手修对）**：max-loss equity **剔除 `settled_interest`**（carry 不得掩盖纯交易回撤——10%/yr 利息会让真实亏损在雷达下失血）；available-pool per-side cap **加注释**说明是保守 per-side 上界（非真·剩余预算，单边 seed 下 ≈ 全额度，安全）；补判别性测试（off-peg mark 两侧 sizing、cancel-all 2 单）杀 qa 存活突变体。

**理由**：真金熔断必须是**可重启幸存的、不可自动续跑的** kill-switch（ai-quant-validation production gate：reset 须人工 root-cause；自动重启 ≠ 人工介入）；上 mainnet 必须**强制带护栏**（不能靠默认旋钮裸跑）。
**取舍**：`_halted`/`_start_equity` 持久化触及 persistence（D12 曾列为铁律"不动"），但对抗审证明"不动 persistence"才是漏洞——以 additive `.get` + schema 不 bump 把回归面压到最小（旧快照精确 round-trip 不变）。
参见 D12（3b 主决策）、D10/R1（persistence/对账）；arb-execution-risk / ai-quant-validation / trading-risk-control。

## D14 — Phase 3b 大幅简化：两模式（dryrun|live）+ 唯一资金闸（老板拍板：参数太多）
**背景**：D12/D13 的 3b 把"双确认闸 + per-order cap + total-alloc cap + max-loss 熔断 + 跨重启持久 + mainnet 护栏 + 三道 confirm env"叠成一座参数迷宫。老板判定**参数太多**，拍板砍到最小可用面。D14 是对 D12/D13 的**取代**（不是新增 feature），只动 **mode/资金/闸 配置 + 对应代码/测试**；maker 订单生命周期（reconcile/poll/cancel-to-terminal/persistence v2/markout、GTC 分档双边 ladder）**完全不动**（feedback_multi_mode_parity）。

**最终模型**：
1. **两个模式**：`runtime.mode` 只 `dryrun`（默认）| `live`。`resolve_mode` 返回 dryrun|live，未知值（含旧 `paper`）一律 coerce 到 **dryrun**（安全默认）。`dryrun` = 跑 maker 引擎但**模拟撮合**（沿用原 paper sim-fill）、**不构造 order client、不下真单、不用 key**，markout 仪表照常。`live` = 真实 GTC PostOnly maker 下单（**mainnet 真金**）。
2. **`MODE=live` 一个开关即真金**：无任何额外 confirm env（删 `LIVE_TRADING_CONFIRM` 闸）。缺 key → MakerOrderClient 构造处**自然 RuntimeError**（不预检、不静默降级）。`maker 路径开关 = (mode=='live')`（`_compute_maker_enabled = self.armed`，`armed = live_authorization(mode)= (mode=='live')`）。
3. **唯一资金闸 = `live.max_total_alloc_usd`**（保留 D12 的 seed + reconcile-pool 双入口 sizing 强制；`-1` = 全钱包）。现货**资本封顶 = 损失封顶**，故无需 per-order cap 也无需 PnL max-loss 熔断。

**删除（config + 代码 + 测试）**：`runtime.testnet/maker_enabled/allow_mainnet` + `resolve_testnet/resolve_maker_enabled/resolve_allow_mainnet`；`live.max_order_usd` 全套（`_clamp_to_cap`、`desired_orders` 的 cap 参数、`place_postonly` 硬断言）；`live.max_loss_usd` **整套 PnL max-loss 熔断**（`_check_max_loss`/`_start_equity`/`_guard_mainnet_canary`/banner caps 显示 + D13 为 max-loss 加的 `halted`/`start_equity` 跨重启持久化与 `_guard_resumed_halt`）；env `LIVE_TRADING_CONFIRM`/`LIVE_MAINNET_CONFIRM`/`LIVE_UNCAPPED_CONFIRM`/`LIVE_CLEAR_HALT`；MakerOrderClient 的 testnet/allow_mainnet 门（ctor/place 拒 mainnet、sandbox 标志）→ live 直接 mainnet 真实构造。

**保留（3a 订单生命周期安全，≠ max-loss）**：`_halt_operator_reconcile`（不可归属成交/撤单不达终态/reject-streak/落盘失败的 operator halt，**in-memory** 标志，重启由 R1 对账重新检测——不再持久化）、`_cancel_to_terminal`、`_cancel_all_resting`（退出/halt 撤单 kill-switch）、cancel-to-terminal 轮询、fail-closed 落盘 halt。

**理由**：参数面 = 误用面。两模式 + 单一资本闸把"会不会误上真金/裸跑"压成一个可判别问题（`MODE=live` 与否），并由判别性测试钉死（`test_default_mode_is_dryrun`、`test_dryrun_default_never_builds_client_never_places`、`test_live_builds_client_and_can_place`）。现货资本封顶即损失封顶，max-loss 熔断与 per-order cap 对一个**已 alloc-capped** 的现货 maker 是冗余复杂度。
**取舍**：① `MODE=live` 单闸不再有"第二道人手确认"——靠 default=dryrun + coerce-unknown-to-dryrun + 缺 key 即 RuntimeError 防误触；真金 canary 由 owner 显式 `MODE=live` + 充值 dedicated 子账户到 `max_total_alloc_usd` 额度执行。② operator halt 不再跨重启持久（回到 3a 行为）——`paper`/dryrun 容器 `restart: unless-stopped` 对**模拟**无害；真金走裸机 `sca live`（无自动重启），halt 即停。③ D12/D13 的双确认/max-loss/持久化护栏被本决策取代——不是它们错，是**老板判定复杂度 > 收益**。
参见 D12/D13（被本决策取代的 3b 原模型）、D8（旧三重闸亦被简化）、D10/R1（persistence/对账，未动）、D11（maker 执行模型，未动）；feedback_multi_mode_parity。

## D15 — 真金上线前安全补丁：状态文件按 mode 隔离 + 清理 D14 遗留过期文案 + canary cap=1000
**背景**：D14 落地后做"上真金"前的最后一遍走查，发现三处会咬人的隐患：① 持久化路径**不分 mode**（`<symbol>_state.json`），dryrun 跑完直接起 live 时 `_maybe_resume` 会加载 dryrun 的**模拟**状态（R1 对账虽会安全拒，但脆且烦）；② 两条文案是 3a/复杂 3b 的旧话，对真金操作**误导**；③ 唯一资金闸 shipped 默认仍是 `-1`（全钱包）。本补丁**不动 maker 订单生命周期**（feedback_multi_mode_parity），只动 persistence 路径、两条文案、一个配置值 + 对应测试。

1. **状态文件按 mode 隔离（核心，防 dryrun→live 污染）**：`persistence.save_state/load_state/append_event/read_events` 加可选 `tag: str = ""`。`tag==""` ⇒ 旧路径 `<symbol>_state.json`（**向后兼容**：standalone dryrun 工具 + 直接单测不受影响）；`tag` 非空 ⇒ `<symbol>_<tag>_state.json`（events 同理）。引擎把**解析后的 `self.mode`**（dryrun|live）作为 tag 传给**全部** 8 个 persistence 调用点 ⇒ live 引擎只读写 `USD1USDT_live_state.json`，dryrun 只 `USD1USDT_dryrun_state.json`，两者**永不共用文件**；同 mode 重启仍能找回自己的文件。旧的无 tag 遗留文件被 mode 化引擎直接忽略（它只找 `_<mode>_`）——正是目的，**无需迁移**（这是首次 live 前）。
2. **清理 D14 遗留的两条过期文案（engine.py，误导真金操作）**：① seed 注释原"Scoped to maker_enabled (=> testnet), so it is impossible on mainnet" 是旧话——D14 后 `maker_enabled == (mode==live)`，故 live(mainnet) 无本地状态启动时，seed 从已充值的专用子账户建初始仓 → reconcile 走 'proceed'；混合/歧义余额或任何挂单 = 乱状态 → refuse。② fresh_deploy 拒绝消息原"real order placement is NOT built (Phase 3) ... wait for Phase 3" **是错的**（3b 早建好下单路径）。真实原因：reconcile 批了 fresh deploy，但我们**绝不盲目建 config 大小的仓**（架空 R1、跟真实余额对不上）；初始仓必须来自 seed-from-balance（把专用子账户充成干净单一币种 → reconcile 'proceed'）。命中此条 = 余额空/混合/歧义。**拒绝条件不变，只改文案**。
3. **canary cap**：`live.max_total_alloc_usd` shipped 默认 `-1`（全钱包）→ **`1000`**（老板的 canary 本金）。sizing 强制路径（seed + reconcile available-pool 双入口，D12/D14）不变；现货资本封顶即损失封顶。

**理由**：上真金前必须保证"dryrun 的模拟状态绝无可能被 live 加载"（按 mode 隔离文件是比"靠 R1 拒"更前置、更确定的一道闸），且任何对操作员撒谎的文案（"等 Phase 3"）在真金面前都是事故源；canary 默认值给 1000 让"忘了配 cap 直接上"也只在小额暴露。
**取舍**：① 隔离用文件名 tag 而非目录/迁移——zero 迁移、向后兼容（空 tag 保留旧路径供 standalone 工具与直接单测），代价是 ~21 个假设无 tag 路径的既有 resume/maker 测试改成显式 `"dryrun"` tag（**断言更新，非行为变更**）。② cap=1000 是数据默认，owner 仍可按实际 canary 额度覆写并精确充值 dedicated 子账户（over-funded 会在 R1 dedicated 对账处保守 refuse）。
参见 D14（两模式 + 唯一资金闸，本补丁在其上加固）、D10/R1（persistence/对账，路径加 tag 未动语义）、D11（maker 执行模型，未动）；feedback_multi_mode_parity。
