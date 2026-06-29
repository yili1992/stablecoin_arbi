"""backtest() per-symbol 参数化 — symbol/params 三层优先级 + USD1 回归保证。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from sca.backtest import strategy as S

N1 = {"rungs": [1], "fractions": [1.0], "min_profit_bp": 1, "rest_bps": 14,
      "anchor_ema_span": 21, "rebuy_offset_bp": -1, "interest_apr": 0.0}
N5 = {"rungs": [1, 2, 3, 4, 5], "fractions": [0.15, 0.18, 0.20, 0.22, 0.25],
      "min_profit_bp": 1, "rest_bps": 14, "anchor_ema_span": 21,
      "rebuy_offset_bp": -1, "interest_apr": 0.0}


def test_explicit_params_take_effect():
    df = S.load("USD1USDT")
    r1 = S.backtest(0.0, params=N1, with_yield=False, df=df)["apr"]
    r5 = S.backtest(0.0, params=N5, with_yield=False, df=df)["apr"]
    assert r1 != r5  # 不同参数 → 不同结果（N1 单档 vs N5 ladder）


def test_no_args_follows_yaml_global_sell_round():
    # P1 (Codex): 无 symbol/params → 模块全局 fallback 必须跟 yaml strategy 块(现
    # sell_round=floor). 否则默认研究回测(main 报告 / sweep)走 legacy round != 实盘 floor
    # = 口径漂移, 破坏 backtest==live. 无参必须与 symbol/live 同口径(floor).
    df = S.load("USD1USDT")
    r = S.backtest(0.0, with_yield=False, fill_mode="touch", df=df)["apr"]
    assert abs(r - 3.891) < 0.001  # yaml floor 口径(实测), 与 symbol/live 一致


def test_no_args_equals_symbol_usd1_unified_floor():
    # P1 fix 后: 无参 fallback 与 symbol=USD1 都读 yaml floor → 口径统一(backtest 内部无漂移).
    df = S.load("USD1USDT")
    r_glob = S.backtest(0.0, with_yield=False, df=df)["apr"]
    r_sym = S.backtest(0.0, symbol="USD1USDT", with_yield=False, df=df)["apr"]
    assert abs(r_glob - r_sym) < 1e-9     # 同 yaml floor 口径, 无 fallback 漂移


def test_legacy_round_params_rollback_baseline():
    # 回滚基线锚: 显式 legacy round params (sell_round=round + margin=0) → 旧 2.661.
    # 证明"传 legacy params / 删 yaml 两行 = 回到改造前 round 口径".
    df = S.load("USD1USDT")
    legacy = {**N5, "sell_round": "round", "min_sell_margin_bp": 0.0}
    r = S.backtest(0.0, params=legacy, with_yield=False, fill_mode="touch", df=df)["apr"]
    assert abs(r - 2.661) < 0.001


def test_params_n1_matches_explicit_n1_via_load():
    # params 路径与 df 路径一致性：N1 单档应显著高于 N5（之前实测 ~2x）
    df = S.load("USD1USDT")
    r1 = S.backtest(0.0, params=N1, with_yield=False, df=df)["apr"]
    assert r1 > 4.0  # USD1 N1 adv0 排除生息 ≈ 4.98


def test_backtest_live_sell_price_same_source():
    # plan 场景7: backtest 与 live(desired_orders) 卖价同源同值(final_sell_price 单一真源)。
    # 抓"一方漏传 sell_round/margin"的口径漂移 bug。
    from sca.strategy_rules import final_sell_price
    from sca.live.order_recon import desired_orders
    anchor, entry, rung, tick = 1.00116, 1.0010, 1, 1e-4
    for sr, margin in (("floor", 2.0), ("ceil", 0.0), ("round", 0.0)):
        slices = [{"state": "usd1", "qty": 10.0, "cash": 0.0, "entry": entry}]
        live_px = desired_orders(anchor, slices, [rung], -1, tick, 1e-6,
                                 1000.0, 1000.0, 0.0, 0.0,
                                 min_profit_bp=1.0, rest_bps=14.0,
                                 sell_round=sr, min_sell_margin_bp=margin)[0].price
        bt_px = final_sell_price(anchor, rung, entry, 1.0, 14.0, tick,
                                 sell_round=sr, min_sell_margin_bp=margin)
        assert live_px == bt_px, f"{sr}/{margin}: live {live_px} != backtest {bt_px}"


def test_backtest_floor_round_differ_proving_sell_round_wired():
    # backtest 循环真响应 sell_round(非硬编码 round): floor+2bp 改变成交 -> APR 不同
    df = S.load("USD1USDT")
    p_round = {**N5, "sell_round": "round", "min_sell_margin_bp": 0.0}
    p_floor = {**N5, "sell_round": "floor", "min_sell_margin_bp": 2.0}
    apr_round = S.backtest(0.0, params=p_round, with_yield=False, fill_mode="touch", df=df)["apr"]
    apr_floor = S.backtest(0.0, params=p_floor, with_yield=False, fill_mode="touch", df=df)["apr"]
    assert apr_round != apr_floor
