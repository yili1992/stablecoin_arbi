# experiments/ — strategy-search exploration (evidence trail)

These are the artifacts from the multi-agent ("ultracode") strategy optimization. They are kept
as the **evidence trail**, not as polished/supported code. The supported, runnable entry points
live at the repo root (`strategy.py`, `backtest_engine.py`, `sweep.py`, `tools/dryrun.py`).

Contents:
- `bt_faithful.py` — the faithful backtest engine the variants build on (same as repo-root
  `backtest_engine.py`).
- `bt_variant_r1_*.py` — the 8 candidate strategy designs that were generated and backtested.
- `bt_verify_r1_*.py` — **independent from-scratch re-implementations** used to adversarially
  verify each "beats-10%" claim. This is what killed the overfit/illiquid false winners
  (e.g. `bt_paal.py` / `bt_variant_r1_7.py`'s 11.93% headline) and confirmed only `r1_6`.
- `bt.py`, `bt_paal.py`, `bt_ladder*.py`, `grid_variant.py`, `sweep*.py`, `validate.py`,
  `finalize.py` — assorted scratch from the search.
- `workflow_optimize.js` — the workflow script that orchestrated the whole search
  (8 variants → backtest → adversarial verify → loop → synthesize).

To run any of these, `cd experiments/` first (they use `../data` and import sibling modules).

The winner `r1_6` was promoted, cleaned, and re-derived as the repo-root `strategy.py`.
See `docs/FINDINGS.md` for how the search concluded (short version: only `r1_6` survived, and
even it does not beat buy-and-hold out-of-sample).
