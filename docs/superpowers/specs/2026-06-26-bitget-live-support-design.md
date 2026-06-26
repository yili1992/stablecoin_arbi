# Bitget Live Support — 设计 Spec

> 状态：待老板审 · 日期 2026-06-26 · 分支 `worktree-bitget-live-support`

## 1. 目标与决策

**目标**：支持 per-symbol exchange —— **USDC 在 Bitget、USD1 在 Bybit** 各自做市（GTC PostOnly maker, 0-fee）。

**老板决策**：直接做完整 Bitget live（跳过 dryrun-only 先行验证）。
**已知风险（老板知情自担）**：Bitget USDC 回测 9.97% 是 touch 口径、脆弱（两所 0.57bp 价差→80% 年化差，1bp 档微观敏感），真实成交率未经 live 验证。

**一个简化**：USDC 挪到 Bitget 后，Bybit 只剩 USD1（单 symbol）→ 之前卡住的"一个账户双跑 reconcile 改动"**不再需要**，各所各自单 symbol、EXACT 对账干净。

## 2. 架构

当前 engine 硬编码 Bybit：feed（`engine.py:122` WS `stream.bybit.com` + `:252` REST kline + `:1939` WS 协议）+ client（`orders.py:89`/`bybit_client.py:136` `ccxt.bybit`）。

**重构为 Exchange Adapter 抽象**：

```
src/sca/live/exchanges/
  base.py     — ExchangeAdapter 接口
  bybit.py    — BybitAdapter (现有逻辑搬入, 行为零变化)
  bitget.py   — BitgetAdapter (新)

ExchangeAdapter 接口:
  # 行情 feed
  async ws_quotes() -> 推送 (bid, ask)        # WS, 各所协议不同
  rest_kline(symbol, interval, limit) -> bars  # REST, 各所 url 不同
  # 交易 (ccxt 已抽象, 主要差 params/type)
  make_client() -> ccxt exchange               # ccxt.bybit / ccxt.bitget
  fetch_balance_coins() -> {COIN:{wallet}}     # 各所 balance 格式→统一 (reconcile 用)
  order_params(link_id) -> dict                # postOnly + clientOrderId 各所字段
  taker/maker_fee(symbol) -> 0.0               # 稳定币 0-fee (ccxt 默认值不可信, 硬编码 0)
```

engine 通过 `adapter = adapter_for(symbol)` 拿到对应所的 feed+client，不再直接碰 `stream.bybit.com` / `ccxt.bybit`。

## 3. 改动文件

| 文件 | 改动 |
|---|---|
| `exchanges/base.py` `bybit.py` `bitget.py` | **新增** adapter 抽象 + 两所实现 |
| `engine.py` | feed/REST/WS → `adapter`; client → `adapter` |
| `orders.py` | `ccxt.bybit` → `adapter.make_client()` + `adapter.order_params()` |
| `bybit_client.py` | 逻辑搬进 `exchanges/bybit.py`（balance map） |
| `config.py` | 加 `exchange_for(symbol)`（读 `universe[symbol].exchange`） |
| `creds.py` | per-exchange keys：`BYBIT_*` / `BITGET_*` |
| `config/strategy.yaml` | `universe[USDCUSDT].exchange: bitget` + Bitget WS/REST url |
| `docker-compose.yml` | `bot-usdc` → Bitget（`BITGET_API_KEY`，连 Bitget） |
| `tests/` | per-exchange adapter 测试 + 回归 |

## 4. 分阶段（降风险，每阶段可验证）

### Phase 1：Exchange 抽象 + Bitget feed
- 抽 `ExchangeAdapter` 接口；Bybit 逻辑搬入 `BybitAdapter`（**行为零变化，回归 pin 386 测试**）
- `BitgetAdapter` feed：Bitget WS（订阅 books/ticker）+ REST kline（`/api/v2/spot/market/candles`）
- **验证**：Bitget **dryrun** 能跑（模拟撮合 off Bitget feed）→ 实测 1bp 档 markout（这是 N1 脆弱性的真验证，即使老板选完整 live，Phase 1 dryrun 数据是免费的早期信号）

### Phase 2：Bitget 交易（orders/balance/reconcile）
- `BitgetAdapter` client：ccxt bitget `create_order`（postOnly）/ `fetch_balance`（spot）/ `fetch_open_orders`
- `fetch_balance_coins`：Bitget spot balance → 统一 `{coin:{wallet}}`
- reconcile：per-exchange EXACT（base=USDC, quote=USDT；单 symbol 一账户，EXACT 干净）
- **0-fee 硬编码**：`maker_fee=0` for 稳定币（ccxt 默认 0.1% 不可信，已验证）
- PostOnly 确认：Bitget ccxt `params={'postOnly':True}` 或等效

### Phase 3：per-symbol exchange 集成
- `universe[USDCUSDT].exchange: bitget`；engine `adapter_for(symbol)` 路由
- `creds.py` per-exchange；`docker-compose` bot-usdc → Bitget
- 端到端：USD1@Bybit + USDC@Bitget 各自 live；dashboard tab 两所

## 5. 关键风险 / 回归

- **Bitget WS 协议**（最大未知）：订阅消息 + 数据格式与 Bybit 完全不同，需逆向 Bitget WS 文档/实测
- **balance/reconcile 口径**：Bitget spot vs Bybit UTA（`{"type":"unified"}`）格式不同
- **PostOnly 支持**：确认 Bitget ccxt 能下 PostOnly maker（否则做市 edge 不成立）
- **0-fee 持续性**：Bitget USDC 0-fee 是促销还是制度（促销取消则 edge 消失）—— 运维确认
- **回归硬保证**：Bybit 路径（USD1）零变化，adapter 重构后 **386 测试全绿 + USD1 backtest=2.661 pin**

## 6. 工作量与执行

~1-2 周。Phase 1（feed adapter）最大、最不确定（Bitget WS 逆向）。建议**分发模式**：per-phase dispatch dev-worker subagent，协调者 review。每 phase 完成后老板验证再进下一 phase。
