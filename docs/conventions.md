# Conventions

> 由 agent 工作过程中自动维护，记录对项目的持续理解。最后更新：2026-06-14

## 配置生效路径
- `config/strategy.yaml` 是所有参数的**唯一来源**；代码通过 `sca.config.CFG` 读取，**改参数改 yaml，不改代码**。
- `strategy.*`(rungs/fractions/anchor_ema_span/rebuy_offset) 驱动推荐策略；`baseline.*` 驱动原始 freqtrade 策略；`backtest.*` 驱动 alloc/adv 扫描/fill 模型；`sweep.*` 驱动参数扫描；`dryrun.*` 驱动实测工具；`universe`/`market` 全局共用。
- 路径解析：`sca.config` 默认 `REPO_ROOT = parents[2]`；Docker 里用 `SCA_CONFIG`/`SCA_DATA_DIR`/`SCA_OUT_DIR` 显式覆盖，安装模式无关。

## 包结构
- `src/sca/` 是 Python 包；`pip install -e .` 后用 `sca <cmd>`，或 `python scripts/run.py <cmd>`(免安装，自动加 src 到 path)。
- 模块：`data/`(fetch + load) · `backtest/`(engine=原策略, strategy=推荐切片阶梯) · `optimize/`(sweep) · `tools/`(dryrun)。
- 入口统一走 `sca.cli`（用 runpy 跑各模块的 `__main__`，行为等同 `python -m sca.<module>`）。

## 回测保真铁律（不可违反）
- **无 lookahead**：1h EMA 只用已收盘的（`avail_ts = ts + 3600s` 后 merge_asof）。
- **三档成交模型** touch / strict / strict+量门；adverse-selection 必扫 `{0, 0.5, 1.0, 1.5}` bp/side。
- **基准是 mark-to-market 持有（~10.27%）**，不是 flat 10%。
- **任何"跑赢"必须样本外(OOS)成立 + 独立从零重写复现**，否则判过拟合/成交幻觉（PAAL 11.9% 就是这么被毙的）。

## Ship 流程
- 本地 commit ≠ shipped。push main 必须 owner 亲自。
- 上实盘前：服务器 `docker compose --profile dryrun up -d` 实测真实 adverse selection，再决定。
- dryrun 零下单零密钥；真实下单模式未实现，需显式授权 + API key。
