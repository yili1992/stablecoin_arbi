# Decisions

> 关键技术/策略决策及理由。最后更新：2026-06-14

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
