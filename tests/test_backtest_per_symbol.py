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


def test_no_args_uses_module_globals_regression():
    # 关键回归：无 symbol/params → 模块全局(yaml strategy 块 = USD1 N5 floor1 rest14)
    # 改造前实测 USD1 N5 adv0 排除生息 = 2.661，改造后必须 bit-一致
    df = S.load("USD1USDT")
    r = S.backtest(0.0, with_yield=False, fill_mode="touch", df=df)["apr"]
    assert abs(r - 2.661) < 0.001


def test_symbol_usd1_equals_module_globals():
    # USD1 无 override → strategy_for(USD1) == 模块全局 → 两条路径数值一致
    df = S.load("USD1USDT")
    r_sym = S.backtest(0.0, symbol="USD1USDT", with_yield=False, df=df)["apr"]
    r_glob = S.backtest(0.0, with_yield=False, df=df)["apr"]
    assert abs(r_sym - r_glob) < 1e-9


def test_params_n1_matches_explicit_n1_via_load():
    # params 路径与 df 路径一致性：N1 单档应显著高于 N5（之前实测 ~2x）
    df = S.load("USD1USDT")
    r1 = S.backtest(0.0, params=N1, with_yield=False, df=df)["apr"]
    assert r1 > 4.0  # USD1 N1 adv0 排除生息 ≈ 4.98
