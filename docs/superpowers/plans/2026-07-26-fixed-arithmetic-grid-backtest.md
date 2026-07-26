# Fixed Arithmetic Grid Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a clean-room fixed arithmetic spot-grid backtest for USD1/USDT and USDC/USDT, with honest parameter selection, current-strategy and hold comparisons, and live-period replay.

**Architecture:** A pure event-driven grid engine owns cell inventory and fill accounting. A separate public-candle module fetches reproducible Bybit/Bitget windows without changing canonical repository data. A study module selects parameters on IS only, evaluates frozen parameters on OOS/live windows, and writes one JSON report; the existing ladder backtest receives only additive capital/accounting parameters needed for fair comparison.

**Tech Stack:** Python 3.10+, pandas, numpy, ccxt, pytest, existing `DailyMinInterest` and strategy configuration.

---

### Task 1: Make The Existing Ladder Comparison Capital-Aware

**Files:**
- Modify: `src/sca/backtest/strategy.py`
- Modify: `tests/test_backtest_per_symbol.py`

- [ ] **Step 1: Write failing tests for configurable capital and exact ledger fields**

Add tests that run the same fixture at USD 10,000 and USD 1,000, verify linear results when liquidity is infinite, and require precise result keys:

```python
def test_backtest_alloc_usd_scales_ledger_but_not_apr():
    df = S.load("USD1USDT").iloc[:2000].copy()
    big = S.backtest(0.5, symbol="USD1USDT", df=df, alloc_usd=10_000)
    small = S.backtest(0.5, symbol="USD1USDT", df=df, alloc_usd=1_000)
    assert big["apr"] == small["apr"]
    assert abs(big["final_equity"] / small["final_equity"] - 10) < 1e-9
    assert abs(big["settled_interest"] / small["settled_interest"] - 10) < 1e-9
    assert abs(big["return_pct"] - small["return_pct"]) < 1e-9


def test_backtest_exact_ledger_reconciles():
    df = S.load("USD1USDT").iloc[:2000].copy()
    r = S.backtest(0.5, symbol="USD1USDT", df=df, alloc_usd=1_234)
    expected = (r["final_equity"] / 1_234 - 1) * 100
    assert abs(r["return_pct"] - expected) < 1e-12
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run: `PYTHONPATH=src pytest -q tests/test_backtest_per_symbol.py -k 'alloc_usd or exact_ledger'`

Expected: failure because `backtest()` does not accept `alloc_usd` and does not return the ledger fields.

- [ ] **Step 3: Add the optional capital parameter and ledger output**

Change the signature and replace internal uses of `ALLOC` with a local resolved value:

```python
def backtest(adv: float = 0.5, *, symbol: str | None = None,
             params: dict | None = None, with_yield: bool = True,
             fill_mode: str = "touch", liq_gate: float | None = None,
             df: pd.DataFrame | None = None,
             alloc_usd: float | None = None) -> dict:
    alloc = ALLOC if alloc_usd is None else float(alloc_usd)
    if alloc <= 0:
        raise ValueError("alloc_usd must be positive")
```

Use `alloc` for initial slice quantities and all percentage denominators, then add unrounded ledger fields:

```python
settled_interest = interest.settled if interest is not None else 0.0
return_pct = (final / alloc - 1) * 100

# Add these exact entries to the existing result dictionary before returning it.
result.update({
    "final_equity": final,
    "return_pct": return_pct,
    "settled_interest": settled_interest,
    "initial_base_pct": 100.0,
})
return result
```

Keep all existing rounded fields unchanged for backward compatibility.

- [ ] **Step 4: Run focused and existing backtest tests**

Run: `PYTHONPATH=src pytest -q tests/test_backtest_per_symbol.py tests/test_smoke.py`

Expected: all tests pass.

- [ ] **Step 5: Commit the capital-aware comparison support**

```bash
git add src/sca/backtest/strategy.py tests/test_backtest_per_symbol.py
git commit -m "feat(backtest): expose capital-aware ladder ledger"
```

### Task 2: Implement The Fixed Grid State Machine Test-First

**Files:**
- Create: `src/sca/backtest/fixed_grid.py`
- Create: `tests/test_fixed_grid.py`

- [ ] **Step 1: Write fixtures and failing tests for grid construction**

Define a compact OHLC fixture helper and assert symmetric arithmetic cells, 50% initial base allocation, and capital conservation:

```python
def bars(rows):
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "turnover"])


def test_build_grid_has_symmetric_cells_and_conserves_capital():
    state = build_grid(GridConfig(
        center=1.0, step_bp=2, half_width_bp=6, capital=1_000,
        tick_size=0.0001, apr=0.0, adverse_bp=0.0,
    ))
    assert [c.lower for c in state.cells] == pytest.approx(
        [0.9994, 0.9996, 0.9998, 1.0, 1.0002, 1.0004]
    )
    assert [c.upper for c in state.cells] == pytest.approx(
        [0.9996, 0.9998, 1.0, 1.0002, 1.0004, 1.0006]
    )
    assert state.initial_base_pct == pytest.approx(50.0)
    assert state.quote + sum(c.qty * state.effective_entry for c in state.cells) == pytest.approx(1_000)
```

- [ ] **Step 2: Write failing lifecycle tests**

Cover adjacent replacement orders, next-bar eligibility, multiple independent fills, exact-touch strictness, and out-of-range saturation:

```python
def test_replacement_cannot_fill_on_creation_bar():
    df = bars([
        [0, 1.0, 1.0002, 0.9998, 1.0, 1_000_000],
        [300_000, 1.0, 1.0002, 0.9998, 1.0, 1_000_000],
    ])
    r = run_fixed_grid(df, GridConfig(1.0, 1, 1, 1_000, 0.0001, 0.0, 0.0), fill_mode="touch")
    assert r.fills_on_bar[0] == 1
    assert r.completed_cycles == 1


def test_strict_does_not_fill_exact_touch():
    df = bars([[0, 1.0, 1.0001, 0.9999, 1.0, 1_000_000]])
    touch = run_fixed_grid(df, config(), fill_mode="touch")
    strict = run_fixed_grid(df, config(), fill_mode="strict")
    assert touch.fills > strict.fills


def test_price_below_range_saturates_in_base_without_recentering():
    df = falling_bars_through_all_levels()
    r = run_fixed_grid(df, config(half_width_bp=4), fill_mode="strict")
    assert r.terminal_saturation == "base"
    assert r.center == 1.0
```

- [ ] **Step 3: Run the grid tests and verify import failure**

Run: `PYTHONPATH=src pytest -q tests/test_fixed_grid.py`

Expected: failure because `sca.backtest.fixed_grid` does not exist.

- [ ] **Step 4: Implement dataclasses and grid construction**

Create the public types and deterministic builder:

```python
@dataclass(frozen=True)
class GridConfig:
    center: float
    step_bp: int
    half_width_bp: int
    capital: float
    tick_size: float
    apr: float
    adverse_bp: float
    liq_gate: float | None = None


@dataclass
class GridCell:
    index: int
    lower: float
    upper: float
    state: Literal["quote", "base"]
    cash: float
    qty: float
    active_from: int = 0
    bought_cash: float | None = None


@dataclass
class GridState:
    config: GridConfig
    cells: list[GridCell]
    center: float
    lower_bound: float
    upper_bound: float
    effective_entry: float

    @property
    def quote(self) -> float:
        return sum(cell.cash for cell in self.cells)

    @property
    def initial_base_pct(self) -> float:
        base_cost = sum(cell.qty * self.effective_entry for cell in self.cells)
        return base_cost / self.config.capital * 100


def round_to_tick(price: float, tick_size: float) -> float:
    return round(price / tick_size) * tick_size


def build_grid(cfg: GridConfig) -> GridState:
    if cfg.capital <= 0 or cfg.step_bp <= 0 or cfg.half_width_bp < cfg.step_bp:
        raise ValueError("invalid fixed-grid configuration")
    n = cfg.half_width_bp // cfg.step_bp
    step_ticks = max(1, round(cfg.center * cfg.step_bp / 10_000 / cfg.tick_size))
    step = step_ticks * cfg.tick_size
    levels = [round_to_tick(cfg.center + k * step, cfg.tick_size) for k in range(-n, n + 1)]
    budget = cfg.capital / (2 * n)
    effective_entry = cfg.center * (1 + cfg.adverse_bp / 10_000)
    cells = []
    for index, (lower, upper) in enumerate(zip(levels, levels[1:])):
        if upper <= cfg.center:
            cells.append(GridCell(index, lower, upper, "quote", budget, 0.0))
        else:
            cells.append(GridCell(
                index, lower, upper, "base", 0.0, budget / effective_entry
            ))
    return GridState(
        config=cfg,
        cells=cells,
        center=cfg.center,
        lower_bound=levels[0],
        upper_bound=levels[-1],
        effective_entry=effective_entry,
    )
```

- [ ] **Step 5: Implement causal fill processing and accounting**

At each bar, snapshot orders active at bar open. Evaluate buy and sell branches separately when both sides are reached, apply aggregate capacity in deterministic distance order, activate replacements at `bar_index + 1`, and keep the lower-equity branch. Feed base quantity to `DailyMinInterest` before processing fills.

Expose a `GridResult` containing exact equity, return/APR, price PnL, settled carry, MDD, fill/cycle counts, capacity rejects, turnover, allocation time, outside-range time, and terminal saturation. Define price PnL by ledger identity:

```python
price_pnl = final_equity - cfg.capital - settled_interest
```

- [ ] **Step 6: Add adversarial accounting tests**

Add controlled tests proving adverse-selection monotonicity, aggregate capacity, lower-equity branch selection, hourly minimum carry loss, future mutation invariance, and final ledger reconciliation:

```python
def test_adverse_selection_monotonically_reduces_equity():
    df = repeated_two_bar_cycles()
    values = [run_fixed_grid(df, config(adverse_bp=a), "strict").final_equity
              for a in (0, 0.5, 1.0, 1.5)]
    assert values == sorted(values, reverse=True)


def test_result_ledger_reconciles():
    r = run_fixed_grid(repeated_two_bar_cycles(), config(), "strict")
    assert r.final_equity == pytest.approx(
        r.starting_capital + r.price_pnl + r.settled_interest
    )
```

- [ ] **Step 7: Run the complete grid state-machine tests**

Run: `PYTHONPATH=src pytest -q tests/test_fixed_grid.py`

Expected: all tests pass.

- [ ] **Step 8: Commit the fixed-grid engine**

```bash
git add src/sca/backtest/fixed_grid.py tests/test_fixed_grid.py
git commit -m "feat(backtest): add fixed arithmetic grid engine"
```

### Task 3: Add Honest Parameter Selection

**Files:**
- Modify: `src/sca/backtest/fixed_grid.py`
- Modify: `tests/test_fixed_grid.py`

- [ ] **Step 1: Write failing selection tests**

Use a fixture with two candidates and assert selection uses only the supplied training frame, ranks by worst stressed APR, and applies documented tie-breaks:

```python
def test_selection_is_unchanged_when_future_data_changes():
    df = selection_fixture()
    split = len(df) // 2
    selected = select_grid_params(df.iloc[:split], base_config(), [(1, 5), (2, 10)])
    mutated = df.copy()
    mutated.loc[split:, ["open", "high", "low", "close"]] *= 1.1
    selected_again = select_grid_params(mutated.iloc[:split], base_config(), [(1, 5), (2, 10)])
    assert selected.params == selected_again.params


def test_selection_uses_minimum_stressed_apr_not_adv_zero_maximum():
    selected = select_grid_params(selection_fixture(), base_config(), candidates())
    assert selected.params == expected_robust_candidate
```

- [ ] **Step 2: Run tests and verify selection symbols are missing**

Run: `PYTHONPATH=src pytest -q tests/test_fixed_grid.py -k selection`

Expected: failure because `select_grid_params` is undefined.

- [ ] **Step 3: Implement the selector**

Run every valid `(step_bp, half_width_bp)` across adverse selection
`(0.0, 0.5, 1.0, 1.5)` with strict fills and `liq_gate=0.2`. Rank by:

```python
rank_key = (
    min(run.apr for run in stressed_runs),
    -max(abs(run.mdd_pct) for run in stressed_runs),
    -sum(run.fills for run in stressed_runs),
    half_width_bp,
    step_bp,
)
```

Return all candidate scores as well as the selected parameters so the report cannot hide alternatives.

- [ ] **Step 4: Run all fixed-grid tests**

Run: `PYTHONPATH=src pytest -q tests/test_fixed_grid.py`

Expected: all tests pass.

- [ ] **Step 5: Commit parameter selection**

```bash
git add src/sca/backtest/fixed_grid.py tests/test_fixed_grid.py
git commit -m "feat(backtest): select grid parameters on stressed IS"
```

### Task 4: Fetch And Validate Venue-Matched Public Candles

**Files:**
- Create: `src/sca/data/public_candles.py`
- Create: `tests/test_public_candles.py`

- [ ] **Step 1: Write failing pagination and validation tests**

Inject a fake exchange so tests need no network. Assert pagination is ascending, end-exclusive, deduplicated, and computes quote turnover:

```python
def test_fetch_range_pages_deduplicates_and_stops_at_until():
    ex = FakeExchange(pages=[page_0, overlapping_page_1])
    df = fetch_ohlcv_range(ex, "USD1/USDT", "5m", since_ms=0, until_ms=900_000)
    assert df.ts.tolist() == [0, 300_000, 600_000]
    assert df.turnover.tolist() == pytest.approx((df.volume * df.close).tolist())


def test_validate_candles_reports_gaps_and_rejects_bad_ohlc():
    report = validate_candles(gapped_fixture(), timeframe_ms=300_000)
    assert report.gap_count == 1
    with pytest.raises(ValueError, match="non-positive"):
        validate_candles(non_positive_fixture(), timeframe_ms=300_000)
```

- [ ] **Step 2: Run tests and verify module import failure**

Run: `PYTHONPATH=src pytest -q tests/test_public_candles.py`

Expected: failure because `sca.data.public_candles` does not exist.

- [ ] **Step 3: Implement bounded ccxt pagination and validation**

Create `fetch_ohlcv_range`, `validate_candles`, and `write_cache`. The fetcher accepts an exchange instance for testing, advances from the last returned timestamp by one timeframe, retries only `ccxt.NetworkError` and `ccxt.ExchangeNotAvailable` four times, and raises on stalled pagination. Cache writes go only under a caller-provided directory such as `out/research/fixed_grid/candles`.

```python
def make_exchange(exchange_id: str):
    if exchange_id not in {"bybit", "bitget"}:
        raise ValueError(f"unsupported exchange: {exchange_id}")
    return getattr(ccxt, exchange_id)({"enableRateLimit": True})
```

- [ ] **Step 4: Run public-candle tests**

Run: `PYTHONPATH=src pytest -q tests/test_public_candles.py`

Expected: all tests pass without network access.

- [ ] **Step 5: Commit the data module**

```bash
git add src/sca/data/public_candles.py tests/test_public_candles.py
git commit -m "feat(data): fetch venue-matched research candles"
```

### Task 5: Build The Reproducible Grid Study And Report

**Files:**
- Create: `src/sca/backtest/grid_study.py`
- Create: `experiments/run_fixed_grid_study.py`
- Create: `tests/test_grid_study.py`

- [ ] **Step 1: Write failing tests for causal EMA preparation and comparisons**

Assert an hourly close affects the anchor only after that hour closes, Bitget USDC maps to the `BGUSDCUSDT` local files, and all arms receive the same window/capital/adverse assumptions:

```python
def test_hourly_anchor_is_available_only_after_close():
    frame = prepare_ladder_frame(five_minute_fixture(), hourly_fixture_with_jump())
    assert frame.loc[frame.ts < 3_600_000, "ema_anchor"].isna().all()
    assert frame.loc[frame.ts >= 3_600_000, "ema_anchor"].notna().all()


def test_symbol_spec_uses_live_venue_and_capital():
    spec = symbol_spec("USDCUSDT")
    assert spec.exchange_id == "bitget"
    assert spec.local_prefix == "BGUSDCUSDT"
    assert spec.capital == 1_000
    assert spec.apr == 0.0
```

- [ ] **Step 2: Run tests and verify study module is missing**

Run: `PYTHONPATH=src pytest -q tests/test_grid_study.py`

Expected: failure because `sca.backtest.grid_study` does not exist.

- [ ] **Step 3: Implement local data loading and causal frame preparation**

Load USD1 from `USD1USDT_{5m,1h}.csv` and Bitget USDC from
`BGUSDCUSDT_{5m,1h}.csv`. Numeric-convert, sort, validate, calculate EMA21 on
1-hour closes, shift availability by 3,600,000 ms, and merge backward onto 5m
bars. Calculate anchors before splitting so OOS legitimately uses historical
past data.

- [ ] **Step 4: Implement hold and ladder comparison arms**

`run_hold` purchases base at the first open with the same adverse haircut and
uses `DailyMinInterest`. `run_ladder` calls the capital-aware existing backtest.
Both return the common exact fields used by `GridResult`:

```python
COMMON_FIELDS = (
    "final_equity", "return_pct", "apr", "price_pnl",
    "settled_interest", "mdd_pct", "fills", "turnover",
)
```

Idle USDT returns unchanged capital, zero PnL, and zero fills.

- [ ] **Step 5: Implement historical selection and frozen evaluations**

For each symbol, split its local history in half, select from the documented 25
candidates on IS, and evaluate all four arms on OOS for touch/strict and adverse
selection `(0, 0.5, 1.0, 1.5)`. Derive live start/end from
`out/status_<symbol>_live.json`, fetch venue-matched 5m plus sufficient preceding
1h bars, and evaluate the frozen selected parameters without retuning.

If a public fetch exhausts bounded retries, preserve the historical report and
record a structured `live_window_error`; do not silently substitute another
venue or stale local data.

- [ ] **Step 6: Implement JSON output and thin command wrapper**

Write `out/research/fixed_grid/fixed_grid_comparison.json` atomically and print a
table with selected parameters plus OOS/live strict results. The CLI accepts
`--no-fetch` for deterministic local-only tests and `--output` for an alternate
artifact path.

- [ ] **Step 7: Add study integration tests**

Use temporary CSVs and a fake candle fetcher to assert parameter freezing,
artifact schema, no OOS/live influence on selection, exact venue labels, and a
structured fetch failure.

- [ ] **Step 8: Run study and all focused tests**

Run: `PYTHONPATH=src pytest -q tests/test_grid_study.py tests/test_fixed_grid.py tests/test_public_candles.py tests/test_backtest_per_symbol.py`

Expected: all tests pass.

- [ ] **Step 9: Commit the study runner**

```bash
git add src/sca/backtest/grid_study.py experiments/run_fixed_grid_study.py tests/test_grid_study.py
git commit -m "feat(research): compare fixed grid with live strategy"
```

### Task 6: Run The Study And Independent QA Gate

**Files:**
- Create: `out/research/fixed_grid/fixed_grid_comparison.json` (ignored runtime artifact)
- Modify only if verification finds a defect: files from Tasks 1-5

- [ ] **Step 1: Run focused verification with fresh output**

Run: `PYTHONPATH=src pytest -q tests/test_fixed_grid.py tests/test_public_candles.py tests/test_grid_study.py tests/test_backtest_per_symbol.py tests/test_smoke.py`

Expected: all tests pass.

- [ ] **Step 2: Run the complete repository test suite**

Run: `PYTHONPATH=src pytest -q tests`

Expected: all tests pass.

- [ ] **Step 3: Execute the historical and live-period study**

Run: `PYTHONPATH=src python experiments/run_fixed_grid_study.py`

Expected: a terminal comparison table and
`out/research/fixed_grid/fixed_grid_comparison.json`. Public data failures, if
any, appear explicitly in the artifact.

- [ ] **Step 4: Audit result invariants before interpreting returns**

Verify from the JSON artifact that every selected parameter comes from IS,
every OOS/live row uses the frozen value, all ledgers reconcile, APR ordering is
monotonic across adverse selection on controlled tests, and no arm receives a
different venue/window/capital.

- [ ] **Step 5: Run `codex-qa-gate`**

Review the implementation and fresh evidence for money accounting, causal
timing, OHLC ambiguity, capacity, OOS leakage, and whether the tests can detect
the important mutants. Fix any valid finding and rerun the affected checks.

- [ ] **Step 6: Report results without promoting the strategy**

Lead with strict OOS and live-period results, then touch as an upper bound.
Clearly separate pure grid price PnL, carry, initial allocation, and current
ladder/hold opportunity cost. State that candle replay cannot establish maker
queue profitability.
