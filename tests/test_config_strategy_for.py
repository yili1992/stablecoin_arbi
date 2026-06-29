"""per-symbol strategy override resolver — config.strategy_for(symbol).

默认 strategy 块 ← universe[symbol].strategy override（dict-merge）。
无 override 的 symbol 原样返回默认（USD1 行为零变化的回归保证）。
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import pytest
from sca.config import strategy_for

# 一个带 per-symbol override 的注入 cfg（不依赖真实文件，沿用 test_config_runtime pattern）
CFG = {
    "strategy": {
        "rungs": [1, 2, 3, 4, 5],
        "fractions": [0.15, 0.18, 0.20, 0.22, 0.25],
        "min_profit_bp": 1, "rest_bps": 14,
        "anchor_ema_span": 21, "rebuy_offset_bp": -1, "interest_apr": 0.08,
    },
    "universe": [
        {"symbol": "USD1USDT", "apr": 0.08, "kind": "reserve"},          # 无 override
        {"symbol": "USDCUSDT", "apr": 0.0, "kind": "reserve",
         "strategy": {"rungs": [1], "fractions": [1.0], "interest_apr": 0.0}},
    ],
}


def test_no_override_returns_default():
    sp = strategy_for("USD1USDT", cfg=CFG)
    assert sp["rungs"] == [1, 2, 3, 4, 5]
    assert sp["fractions"] == [0.15, 0.18, 0.20, 0.22, 0.25]
    assert sp["interest_apr"] == 0.08


def test_override_applied():
    sp = strategy_for("USDCUSDT", cfg=CFG)
    assert sp["rungs"] == [1]
    assert sp["fractions"] == [1.0]
    assert sp["interest_apr"] == 0.0


def test_partial_override_keeps_defaults_for_unspecified_keys():
    # USDC override 只给了 rungs/fractions/interest_apr → 其余 merge 自默认 strategy 块
    sp = strategy_for("USDCUSDT", cfg=CFG)
    assert sp["min_profit_bp"] == 1
    assert sp["rest_bps"] == 14
    assert sp["anchor_ema_span"] == 21
    assert sp["rebuy_offset_bp"] == -1


def test_unknown_symbol_returns_default():
    sp = strategy_for("ZZZUSDT", cfg=CFG)
    assert sp["rungs"] == [1, 2, 3, 4, 5]


def test_param_defaults_when_no_strategy_block():
    sp = strategy_for("USD1USDT", cfg={"universe": [{"symbol": "USD1USDT"}]})
    assert sp["rungs"] == [5, 7, 10, 14, 20]      # _STRATEGY_PARAM_DEFAULTS
    assert sp["anchor_ema_span"] == 21
    assert sp["interest_apr"] == 0.10


def test_invariant_fractions_sum_not_one():
    bad = {"strategy": {"rungs": [1, 2], "fractions": [0.3, 0.3]}}  # 和=0.6≠1
    with pytest.raises(AssertionError):
        strategy_for("USD1USDT", cfg=bad)


def test_invariant_rungs_fractions_length_mismatch():
    bad = {"strategy": {"rungs": [1, 2, 3], "fractions": [1.0]}}    # 长度不一致(和=1)
    with pytest.raises(AssertionError):
        strategy_for("USD1USDT", cfg=bad)


def test_usd1_matches_real_cfg_strategy_block_regression():
    # 回归保证：真实 CFG 下 USD1 无 override → strategy_for == strategy 块原值
    from sca.config import CFG as REAL_CFG
    sp = strategy_for("USD1USDT", cfg=REAL_CFG)
    st = REAL_CFG.get("strategy", {})
    assert sp["rungs"] == st.get("rungs")
    assert sp["fractions"] == st.get("fractions")
    assert sp["anchor_ema_span"] == st.get("anchor_ema_span", 21)


def test_sell_round_and_margin_default_when_unset():
    # 全局 strategy 未设这两键 → 回滚安全默认 None / 0.0（各调用点回退原口径）
    sp = strategy_for("USD1USDT", cfg=CFG)
    assert sp["sell_round"] is None
    assert sp["min_sell_margin_bp"] == 0.0


def test_sell_round_and_margin_from_global_strategy():
    cfg = {"strategy": {**CFG["strategy"], "sell_round": "floor", "min_sell_margin_bp": 2},
           "universe": CFG["universe"]}
    sp = strategy_for("USD1USDT", cfg=cfg)
    assert sp["sell_round"] == "floor"
    assert sp["min_sell_margin_bp"] == 2


def test_sell_round_margin_inherited_by_override_symbol():
    # USDC 有 rungs/fractions override 但没设这两键 → 继承全局 floor/2（两 live 标的都改）
    cfg = {"strategy": {**CFG["strategy"], "sell_round": "floor", "min_sell_margin_bp": 2},
           "universe": CFG["universe"]}
    sp = strategy_for("USDCUSDT", cfg=cfg)
    assert sp["sell_round"] == "floor"
    assert sp["min_sell_margin_bp"] == 2
