"""PaperEngine per-symbol 参数 — USDC 用 N1 override, USD1 用默认 N3(3档均等, 2026-07-13 从 N5 减)。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from sca.live.engine import PaperEngine


def test_engine_usdc_uses_n1_override(tmp_path):
    eng = PaperEngine(symbol="USDCUSDT", mode="dryrun", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    assert eng.rungs == [1]
    assert eng.fracs == [1.0]
    assert eng.interest_apr == 0.0      # USDC 无息 override
    assert eng.n == 1


def test_engine_usd1_uses_default_n3_regression(tmp_path):
    eng = PaperEngine(symbol="USD1USDT", mode="dryrun", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    assert eng.rungs == [1, 2, 3]                     # 3 档(2026-07-13 从 5 档减, 去掉少触达 4/5bp)
    assert eng.fracs == [0.34, 0.33, 0.33]            # 均等(两口径稳健, 不赌 touch/gate 口径; 老板 2026-07-13)
    assert eng.interest_apr == 0.06     # yaml strategy.interest_apr 默认(2026-07-13 从 0.08 下调)
    assert eng.anchor_ema_span == 21
    assert eng.rebuy_off_bp == -1
    assert eng.min_profit_bp == 1
    assert eng.rest_bps == 14


def test_engine_k_uses_per_symbol_anchor_span(tmp_path):
    # _k 由 self.anchor_ema_span 计算（回归：span21 → 2/22）
    eng = PaperEngine(symbol="USD1USDT", mode="dryrun", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    assert abs(eng._k - 2.0 / 22) < 1e-12
