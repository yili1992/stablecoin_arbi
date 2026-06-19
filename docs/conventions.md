# Conventions

> 由 agent 工作过程中自动维护，记录对项目的持续理解。最后更新：2026-06-19

## 配置生效路径
- `config/strategy.yaml` 是所有参数的**唯一来源**；代码通过 `sca.config.CFG` 读取，**改参数改 yaml，不改代码**。
- `strategy.*`(rungs/fractions/anchor_ema_span/rebuy_offset) 驱动推荐策略 **以及 paper/live 引擎**（paper == backtest，规则同源）；`baseline.*` 驱动原始 freqtrade 策略；`backtest.*` 驱动 alloc/adv 扫描/fill 模型；`sweep.*` 驱动参数扫描；`dryrun.*` 驱动实测工具；`runtime.mode`(dryrun|live) + `live.*`(api_key_env/api_secret_env/max_total_alloc_usd/persist) 驱动引擎模式与唯一资金闸（D14）；`universe`/`market` 全局共用。
- 路径解析：`sca.config` 默认 `REPO_ROOT = parents[2]`；Docker 里用 `SCA_CONFIG`/`SCA_DATA_DIR`/`SCA_OUT_DIR` 显式覆盖，安装模式无关。

## 包结构
- `src/sca/` 是 Python 包；`pip install -e .` 后用 `sca <cmd>`，或 `python scripts/run.py <cmd>`(免安装，自动加 src 到 path)。
- 模块：`data/`(fetch + load) · `backtest/`(engine=原策略, strategy=推荐切片阶梯) · `optimize/`(sweep) · `live/`(engine=paper/live 引擎) · `tools/`(dryrun + dashboard)。
- 入口统一走 `sca.cli`（用 runpy 跑各模块的 `__main__`，行为等同 `python -m sca.<module>`）。`paper` 与 `live` 命令都映射到 `sca.live.engine`，靠 `--mode` 区分（dryrun|live，默认 dryrun；`sca live` 注入 `--mode live`）。

## 回测保真铁律（不可违反）
- **无 lookahead**：1h EMA 只用已收盘的（`avail_ts = ts + 3600s` 后 merge_asof）。
- **三档成交模型** touch / strict / strict+量门；adverse-selection 必扫 `{0, 0.5, 1.0, 1.5}` bp/side。
- **基准是 mark-to-market 持有（~10.27%）**，不是 flat 10%。
- **任何"跑赢"必须样本外(OOS)成立 + 独立从零重写复现**，否则判过拟合/成交幻觉（PAAL 11.9% 就是这么被毙的）。

## Dryrun / live 引擎（live/engine.py）
- **dryrun 是各处默认**：用 Bybit PUBLIC WS（orderbook.1/publicTrade/kline.5/kline.60）+ REST 引导历史 K 线，按 backtest 同源的切片阶梯规则**模拟成交**，零下单零密钥。`dryrun == backtest`（sim-fill）。
- 引擎每 ~10-15s 原子写（tmp+rename）`<SCA_OUT_DIR>/status_<symbol>.json`，dashboard 读它。JSON 必须合法：用 `null`，**禁止 NaN/Infinity**。
- **崩溃安全持久化 / 重启 resume（D10，`live.persist` 默认 on）**：除 status 外，引擎在**每笔成交** + 每次 status 写时同步原子写 `<SCA_OUT_DIR>/<symbol>_state.json`（完整可重建态：slices/realized/计息内部态/start/anchor/history）并 append 成交到 `<symbol>_events.jsonl`（only 增不截断的审计/CSV 源）。重启 `_maybe_resume()` 读快照续跑，不再清盘。**`SCA_OUT_DIR` 必须落在持久化挂载上**（compose 的 `./out:/app/out` 即是）。`--seconds 0` = 永久跑。`persist=false` 回退旧的纯内存行为。**live 红线**：本地态对真实下单不充分，接真实 order 前启动必须先与交易所对账（见 decisions.md D10 / plan R1）。
- **两模式安全模型（D14）**：`MODE=live`（或 runtime.mode=live）**一个开关即真金**（mainnet 真实下单），无额外 confirm env；缺 `BYBIT_API_KEY`/`BYBIT_API_SECRET` → 构造 order client 时**自然报错**（绝不无 key 下单）。唯一资金闸 = `live.max_total_alloc_usd`（现货资本封顶 = 损失封顶，-1=全钱包）。默认 dryrun 模拟成交、不构造 client、零密钥，绝不会误下单。
- dashboard 现为**中文 + K 线蜡烛图**：展示仓位/切片状态、指标（浮动 EMA 锚、卖出 rung、rebuy 线）、PnL、成交质量（markout）。**不得暗示稳赚**——该策略仅样本内薄胜持有、样本外不胜。

## Ship 流程
- 本地 commit ≠ shipped。push main 必须 owner 亲自。
- 上实盘前：服务器跑 dryrun 引擎（`docker compose up -d --build` 起 dryrun + dashboard）看真实成交质量（ROUND-TRIP markout）与仓位演化，再决定。
- dryrun 零下单零密钥；live 真金走 `MODE=live` + API key（D14 单开关），真金 canary 用裸机 `sca live`（无 docker 自动重启），资金由 `live.max_total_alloc_usd` 封顶。
